import os
import uuid
import aiofiles
import httpx
import asyncio
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from docx import Document

app = FastAPI()

# 🔑 Configuración Fireflies
FIREFLIES_API_KEY = os.getenv("FIREFLIES_API_KEY")
FIREFLIES_GRAPHQL_URL = "https://api.fireflies.ai/graphql"

# Carpeta temporal para guardar audios
TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)


# ==============================
# 📌 Guardar audio en servidor
# ==============================
async def save_temp_file(upload_file: UploadFile) -> str:
    file_id = str(uuid.uuid4())
    file_path = os.path.join(TEMP_DIR, f"{file_id}.mp3")

    async with aiofiles.open(file_path, "wb") as out_file:
        content = await upload_file.read()
        await out_file.write(content)

    return file_path


# ==============================
# 📌 Subir audio a Fireflies
# ==============================
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
                "variables": {"input": {"url": audio_url, "title": title}}
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

        return title  # usamos title para buscar después


# ==============================
# 📌 Buscar transcript por título
# ==============================
async def find_transcript_by_title(title: str):
    query = """
    query {
      transcripts {
        id
        title
        status
        createdAt
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
            json={"query": query}
        )

        if response.status_code != 200:
            return None

        data = response.json()
        if "errors" in data:
            return None

        transcripts = data["data"]["transcripts"]
        for t in transcripts:
            if t["title"] == title:
                return t["id"]

    return None


# ==============================
# 📌 Obtener contenido del transcript
# ==============================
async def get_transcript_text(transcript_id: str) -> str:
    query = """
    query GetTranscript($id: String!) {
      transcript(id: $id) {
        status
        words {
          text
        }
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
            json={"query": query, "variables": {"id": transcript_id}}
        )

        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Error transcript: {response.text}")

        data = response.json()
        if "errors" in data:
            raise HTTPException(status_code=500, detail=f"Error transcript: {data['errors']}")

        transcript = data["data"]["transcript"]
        if transcript["status"] != "completed":
            return None

        text = " ".join([w["text"] for w in transcript["words"]])
        return text


# ==============================
# 📌 Endpoint: subir audio y obtener docx
# ==============================
@app.post("/fireflies/subir_audio/")
async def subir_audio(file: UploadFile = File(...), title: str = Form(...)):
    # 1. Guardar temporal
    file_path = await save_temp_file(file)

    # 2. Crear URL pública (Render sirve /temp_audio)
    public_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/temp_audio/{os.path.basename(file_path)}"
    print(f"URL pública generada: {public_url}")

    # 3. Subir a Fireflies
    upload_title = await upload_audio_to_fireflies_via_url(public_url, title)

    # 4. Polling para obtener transcript ID
    transcript_id = None
    for _ in range(30):  # 30 intentos x 5s = 150s
        transcript_id = await find_transcript_by_title(upload_title)
        if transcript_id:
            break
        await asyncio.sleep(5)

    if not transcript_id:
        raise HTTPException(status_code=500, detail="No se encontró transcript en Fireflies")

    # 5. Polling hasta obtener texto
    transcript_text = None
    for _ in range(30):
        transcript_text = await get_transcript_text(transcript_id)
        if transcript_text:
            break
        await asyncio.sleep(5)

    if not transcript_text:
        raise HTTPException(status_code=500, detail="El transcript no está listo")

    # 6. Crear DOCX
    doc = Document()
    doc.add_heading(f"Transcripción: {title}", level=1)
    doc.add_paragraph(transcript_text)

    output_path = f"{TEMP_DIR}/{uuid.uuid4()}.docx"
    doc.save(output_path)

    return FileResponse(output_path, filename=f"{title}.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")




