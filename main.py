import os
import httpx
import aiofiles
import uuid
import glob
import asyncio
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from docx import Document
from docx.shared import Pt

app = FastAPI()

FIREFLIES_API_KEY = os.getenv("FIREFLIES_API_KEY")
FIREFLIES_GRAPHQL_URL = "https://api.fireflies.ai/graphql"
PUBLIC_BASE_URL = "https://form-trans-fireflies.onrender.com"

# ======================
# 1. SUBIR AUDIO A FIRELIES
# ======================

async def upload_audio_to_fireflies_via_url(audio_url: str, title: str) -> str:
    mutation = """
    mutation UploadAudio($input: AudioUploadInput!) {
      uploadAudio(input: $input) {
        success
        title
        message
      }
    }
    """

    headers = {
        "Authorization": f"Bearer {FIREFLIES_API_KEY}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            FIREFLIES_GRAPHQL_URL,
            headers=headers,
            json={
                "query": mutation,
                "variables": {
                    "input": {
                        "url": audio_url,
                        "title": title
                    }
                }
            }
        )

        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Error Fireflies: {response.text}")

        data = response.json()
        if "errors" in data:
            raise HTTPException(status_code=500, detail=f"Error Fireflies: {data['errors']}")

        upload_data = data["data"]["uploadAudio"]
        if not upload_data["success"]:
            raise HTTPException(status_code=500, detail=f"Falló Fireflies: {upload_data['message']}")

        return upload_data["title"]

# ======================
# 2. POLL TRANSCRIPCIÓN
# ======================

async def find_transcript_by_title(title: str):
    query = """
    query FindTranscriptByTitle($title: String!) {
      transcripts(search: $title) {
        id
        title
      }
    }
    """

    headers = {"Authorization": f"Bearer {FIREFLIES_API_KEY}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            FIREFLIES_GRAPHQL_URL,
            headers=headers,
            json={"query": query, "variables": {"title": title}}
        )

        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Error buscar transcript: {resp.text}")

        data = resp.json()
        if "errors" in data:
            raise HTTPException(status_code=500, detail=f"Error buscar transcript: {data['errors']}")

        transcripts = data["data"]["transcripts"]
        return transcripts[0]["id"] if transcripts else None


async def check_transcription_status(transcript_id: str):
    query = """
    query Transcript($id: ID!) {
      transcript(id: $id) {
        id
        title
        sentences {
          speaker_name
          text
        }
      }
    }
    """
    headers = {"Authorization": f"Bearer {FIREFLIES_API_KEY}"}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            FIREFLIES_GRAPHQL_URL,
            headers=headers,
            json={"query": query, "variables": {"id": transcript_id}}
        )

        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Error status: {resp.text}")

        data = resp.json()
        if "errors" in data:
            raise HTTPException(status_code=500, detail=f"Error status: {data['errors']}")

        return data["data"]["transcript"]

# ======================
# 3. PROCESAR ARCHIVO .DOCX
# ======================

def process_file_api(file_path: str, output_docx_path: str, mapping: dict = None):
    doc = Document(file_path)
    content = []
    speaker_map = mapping or {}
    interviewee = speaker_map.get("Entrevistado", "Entrevistado")
    interviewer = speaker_map.get("Entrevistador", "Entrevistador")

    # recolectar texto
    for para in doc.paragraphs:
        if ":" in para.text:
            speaker, text = para.text.split(":", 1)
            speaker = speaker.strip()
            text = text.strip()
            if speaker in speaker_map:
                speaker = speaker_map[speaker]
            content.append((speaker, text))

    # reescribir nuevo doc
    new_doc = Document()
    for speaker, text in content:
        if speaker == interviewer:
            p = new_doc.add_paragraph()
            r = p.add_run(f"{speaker}: {text}")
            r.bold = True
        else:
            new_doc.add_paragraph(f"{speaker}: {text}")

    new_doc.save(output_docx_path)

    # exportar .txt
    output_txt_path = output_docx_path.replace(".docx", ".txt")
    with open(output_txt_path, "w", encoding="utf-8") as f:
        for speaker, text in content:
            f.write(f"{speaker}: {text}\n")

    return output_docx_path, output_txt_path

# ======================
# 4. ENDPOINTS FASTAPI
# ======================

@app.post("/fireflies/subir_audio/")
async def subir_audio_fireflies(file: UploadFile = File(...), title: str = Form(...)):
    try:
        temp_dir = "temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        file_token = str(uuid.uuid4())
        extension = os.path.splitext(file.filename)[1]
        temp_path = os.path.join(temp_dir, f"{file_token}{extension}")

        async with aiofiles.open(temp_path, "wb") as out_file:
            content = await file.read()
            await out_file.write(content)

        # ahora la URL incluye extensión
        public_url = f"{PUBLIC_BASE_URL}/temp_audio/{file_token}{extension}"
        print("URL pública generada:", public_url)

        upload_title = await upload_audio_to_fireflies_via_url(public_url, title)

        # esperar transcripción
        transcript_id = None
        for _ in range(30):
            transcript_id = await find_transcript_by_title(upload_title)
            if transcript_id:
                break
            await asyncio.sleep(5)
        if not transcript_id:
            raise HTTPException(status_code=500, detail="No se encontró transcript.")

        transcript = None
        for _ in range(60):
            transcript = await check_transcription_status(transcript_id)
            if transcript and transcript["sentences"]:
                break
            await asyncio.sleep(5)

        if not transcript or not transcript["sentences"]:
            raise HTTPException(status_code=500, detail="Transcripción no completada.")

        # exportar docx
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        output_docx_path = os.path.join(output_dir, f"{file_token}_final.docx")
        new_doc = Document()
        for s in transcript["sentences"]:
            p = new_doc.add_paragraph()
            run = p.add_run(f"{s['speaker_name']}: {s['text']}")
            run.font.size = Pt(11)
        new_doc.save(output_docx_path)

        return JSONResponse({"message": "OK", "download_url": f"/download/{file_token}"})

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir audio: {e}")

@app.get("/temp_audio/{file_token}.{ext}")
async def serve_temp_audio(file_token: str, ext: str):
    path = f"temp_uploads/{file_token}.{ext}"
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"message": "Archivo no encontrado"})
    return FileResponse(path, media_type="application/octet-stream", filename=f"{file_token}.{ext}")

@app.get("/download/{file_token}")
async def download(file_token: str):
    pattern = f"output/{file_token}_final.docx"
    matches = glob.glob(pattern)
    if not matches:
        return JSONResponse(status_code=404, content={"message": "No encontrado"})
    return FileResponse(matches[0], filename=os.path.basename(matches[0]))
