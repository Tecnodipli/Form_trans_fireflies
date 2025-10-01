import os
import io
import re
import time
import uuid
import zipfile
import requests
from pathlib import Path
from datetime import datetime, timedelta
from collections import OrderedDict
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
from docx import Document
from docx.shared import Pt
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

# =========================
# Configuración Fireflies
# =========================
FIREFLIES_API_KEY = os.getenv("FIREFLIES_API_KEY", "<TU_API_KEY>")
FIREFLIES_GRAPHQL_URL = "https://api.fireflies.ai/graphql"

# Cambia esto al dominio real de tu Render
BASE_URL = os.getenv("BASE_URL", "https://tu-app.onrender.com")

# =========================
# Configuración FastAPI
# =========================
ALLOWED_ORIGINS = [
    "https://www.dipli.ai",
    "https://dipli.ai",
    "https://isagarcivill09.wixsite.com/turop",
    "https://isagarcivill09.wixsite.com/turop/tienda",
    "https://isagarcivill09-wixsite-com.filesusr.com",
    "https://www.dipli.ai/preparaci%C3%B3n",
    "https://www-dipli-ai.filesusr.com"
]

app = FastAPI(title="Fireflies + Formateador (Render)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Path("uploads").mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

DOWNLOADS = {}
EXP_MINUTES = 10

# =========================
# Helpers Fireflies
# =========================
def fireflies_query(query: str, variables: dict = None):
    headers = {"Authorization": f"Bearer {FIREFLIES_API_KEY}"}
    r = requests.post(
        FIREFLIES_GRAPHQL_URL,
        headers=headers,
        json={"query": query, "variables": variables or {}}
    )
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Error Fireflies: {r.text}")
    data = r.json()
    if "errors" in data:
        raise HTTPException(status_code=500, detail=f"Fireflies GraphQL error: {data['errors']}")
    return data["data"]

def get_public_url(file_path: Path) -> str:
    """Construye la URL pública usando Render"""
    return f"{BASE_URL}/uploads/{file_path.name}"

# =========================
# Helpers Formateador
# =========================
def normalize_label(lbl: str) -> str:
    return re.sub(r'\s+', ' ', (lbl or '')).strip().casefold()

def ensure_colon_upper(s: str) -> str:
    s = (s or '').strip()
    if not s.endswith(':'):
        s += ':'
    return s.upper()

def clear_paragraph(p: Paragraph):
    p.text = ''

def set_spacing(p: Paragraph, after_pt=12, before_pt=0):
    pf = p.paragraph_format
    pf.space_before = Pt(before_pt)
    pf.space_after = Pt(after_pt)

def write_label_plus_content(p: Paragraph, final_label: str, content: str, bold_label: bool):
    clear_paragraph(p)
    r1 = p.add_run(final_label + ' ')
    r1.bold = bold_label
    r2 = p.add_run(content.strip())
    r2.bold = bold_label
    set_spacing(p)

def apply_global_font(doc: Document, name="Arial", size_pt=12):
    for p in doc.paragraphs:
        for r in p.runs:
            r.font.name = name
            if r._element.rPr is not None:
                r._element.rPr.rFonts.set(qn('w:eastAsia'), name)
            r.font.size = Pt(size_pt)

def process_docx(doc_stream: io.BytesIO, interview_type: str) -> (io.BytesIO, io.BytesIO):
    doc = Document(doc_stream)
    labels_detected = []
    changed = 0

    for p in doc.paragraphs:
        txt = p.text.strip()
        if txt.lower().startswith("speaker"):
            final_label = ensure_colon_upper(txt.split(":")[0])
            write_label_plus_content(p, final_label, txt.split(":")[-1], bold_label=True)
            labels_detected.append(final_label)
            changed += 1

    apply_global_font(doc)

    out_docx = io.BytesIO()
    doc.save(out_docx)
    out_docx.seek(0)

    # Reporte TXT
    report = f"Entrevista: {interview_type}\nEtiquetas: {', '.join(labels_detected)}\nParrafos actualizados: {changed}"
    out_txt = io.BytesIO(report.encode("utf-8"))
    out_txt.seek(0)

    return out_docx, out_txt

# =========================
# Endpoint: Procesar Audio
# =========================
@app.post("/procesar_audio/")
async def procesar_audio(file: UploadFile = File(...), interview_type: str = Form(...)):
    if not file.filename.endswith((".mp3", ".mp4")):
        raise HTTPException(status_code=400, detail="El archivo debe ser .mp3 o .mp4")

    temp_path = Path("uploads") / file.filename
    with open(temp_path, "wb") as f:
        f.write(await file.read())

    audio_url = get_public_url(temp_path)

    mutation = """
    mutation CreateTranscript($audioUrl: String!) {
      createTranscript(audio_url: $audioUrl) {
        id
        status
        transcript_url
        docx_download_url
      }
    }
    """
    data = fireflies_query(mutation, {"audioUrl": audio_url})
    transcript_id = data["createTranscript"]["id"]

    query_status = """
    query GetTranscript($id: ID!) {
      transcript(id: $id) {
        id
        status
        transcript_url
        docx_download_url
      }
    }
    """

    status = "processing"
    docx_url = None
    while status not in ("completed", "failed"):
        time.sleep(10)
        data = fireflies_query(query_status, {"id": transcript_id})
        status = data["transcript"]["status"]
        docx_url = data["transcript"].get("docx_download_url")

    if status == "failed":
        raise HTTPException(status_code=500, detail="Fireflies falló al procesar el audio.")

    if not docx_url:
        raise HTTPException(status_code=500, detail="Fireflies no devolvió un .docx")

    r = requests.get(docx_url)
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail="No se pudo descargar el DOCX de Fireflies")
    doc_stream = io.BytesIO(r.content)

    final_docx, final_txt = process_docx(doc_stream, interview_type)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        zip_file.writestr("transcripcion_formateada.docx", final_docx.getvalue())
        zip_file.writestr("registro_control.txt", final_txt.getvalue())
    zip_buffer.seek(0)

    token = str(uuid.uuid4())
    expiration = datetime.utcnow() + timedelta(minutes=EXP_MINUTES)
    DOWNLOADS[token] = (zip_buffer, expiration)

    return JSONResponse(content={
        "message": "Transcripción lista",
        "token": token,
        "filename": "transcripcion_formateada.zip"
    })

# =========================
# Endpoint de Descarga
# =========================
@app.get("/descargar/{token}")
async def descargar_archivo(token: str):
    now = datetime.utcnow()
    expired = [t for t, (_, exp) in DOWNLOADS.items() if exp <= now]
    for t in expired:
        DOWNLOADS.pop(t, None)

    if token not in DOWNLOADS:
        raise HTTPException(status_code=404, detail="Token inválido o expirado")

    zip_buffer, _ = DOWNLOADS.pop(token)
    response = StreamingResponse(
        io.BytesIO(zip_buffer.getvalue()),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=archivos_formateados.zip"}
    )
    return response
