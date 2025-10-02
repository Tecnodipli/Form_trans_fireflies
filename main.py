# -*- coding: utf-8 -*-
import os
import re
import zipfile
import io
import uuid
import json
import time
import asyncio
import aiofiles
import httpx
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from docx import Document
from docx.shared import Pt
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

# =========================
# 🔑 Configuración Fireflies
# =========================
FIREFLIES_API_KEY = os.getenv("FIREFLIES_API_KEY")
FIREFLIES_GRAPHQL_URL = "https://api.fireflies.ai/graphql"

# Carpeta temporal para guardar audios
TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)

# =========================
# 1) Configuración de formato
# =========================
FONT_NAME = "Arial"
FONT_SIZE_PT = 12
SPACE_AFTER_LABEL_PT = 12

# =========================
# 2) Patrones robustos
# =========================
TIME_ONLY = re.compile(r'^\s*\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*$')
INLINE_LABEL = re.compile(
    r'^\s*(?:\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*)?'
    r'(speaker\s*\d+)\s*:?\s*(\S.+)$', re.IGNORECASE
)
LABEL_ONLY = re.compile(
    r'^\s*(?:\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*)?'
    r'(speaker\s*\d+)\s*:?\s*$', re.IGNORECASE
)

# =========================
# 3) Utilidades de texto/docx
# =========================
def paragraph_text(p: Paragraph) -> str:
    return ''.join(r.text for r in p.runs) if p.runs else p.text

def normalize_label(lbl: str) -> str:
    return re.sub(r'\s+', ' ', (lbl or '')).strip().casefold()

def ensure_colon_upper(s: str) -> str:
    s = (s or '').strip()
    if not s.endswith(':'):
        s += ':'
    return s.upper()

def clear_paragraph(p: Paragraph):
    p.text = ''

def set_spacing(p: Paragraph, after_pt=SPACE_AFTER_LABEL_PT, before_pt=0):
    pf = p.paragraph_format
    if before_pt is not None:
        pf.space_before = Pt(before_pt)
    if after_pt is not None:
        pf.space_after = Pt(after_pt)

def write_label_plus_content(
    p: Paragraph, final_label: str, content: str, bold_label: bool, bold_content: bool, apply_spacing: bool = True,
):
    content = re.sub(r'\s+', ' ', content or '').strip()
    clear_paragraph(p)
    r1 = p.add_run(final_label + ' ')
    r1.bold = bold_label
    r2 = p.add_run(content)
    r2.bold = bold_content
    if apply_spacing:
        set_spacing(p, after_pt=SPACE_AFTER_LABEL_PT)

def bold_whole_paragraph(p: Paragraph):
    if not p.runs:
        if p.text:
            txt = p.text
            clear_paragraph(p)
            r = p.add_run(txt)
            r.bold = True
        return
    for r in p.runs:
        r.bold = True

def is_time_only(p: Paragraph) -> bool:
    return bool(TIME_ONLY.match(paragraph_text(p).strip()))

def is_label_start(p: Paragraph):
    txt = paragraph_text(p)
    m = INLINE_LABEL.match(txt)
    if m:
        return ('inline', m.group(1), m.group(2))
    m = LABEL_ONLY.match(txt)
    if m:
        return ('only', m.group(1), None)
    return None

def apply_global_font(doc: Document, name=FONT_NAME, size_pt=FONT_SIZE_PT):
    try:
        doc.styles['Normal'].font.name = name
        doc.styles['Normal'].font.size = Pt(size_pt)
    except Exception:
        pass
    
    for p in doc.paragraphs:
        for r in p.runs:
            r.font.name = name
            if r._element.rPr is not None:
                r._element.rPr.rFonts.set(qn('w:eastAsia'), name)
            r.font.size = Pt(size_pt)
    
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    for r in p.runs:
                        r.font.name = name
                        if r._element.rPr is not None:
                            r._element.rPr.rFonts.set(qn('w:eastAsia'), name)
                        r.font.size = Pt(size_pt)

def fmt_hms(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

# =========================
# 4) Registro (TXT) y guardado seguro (en memoria)
# =========================
def write_txt_control_report_in_memory(log: dict, turn_logs: list):
    lines = []
    lines.append("REGISTRO DE CONTROL Y PROCESO")
    lines.append("=" * 34)
    lines.append(f"Fecha de procesamiento: {log['ts']}")
    lines.append(f"Archivo de entrada: {log['input_file']}")
    lines.append(f"Párrafos totales: {log['total_paragraphs']}")
    lines.append(f"Etiquetas detectadas: {', '.join(log['labels_detected']) if log['labels_detected'] else '—'}")
    lines.append(f"Párrafos actualizados: {log['changed_count']}")
    lines.append(f"Timestamps detectados: {log['time_only_count']}")
    lines.append(f"Duración TOTAL: {log['exec_total_hms']} ({log['exec_total_seconds']:.3f}s)")
    lines.append(f"Duración de PROCESAMIENTO: {log['exec_processing_hms']} ({log['exec_processing_seconds']:.3f}s)")
    lines.append("")
    lines.append("Mapeo aplicado (detectada -> final | turnos):")
    for k, raw in log['mapping_raw_order']:
        final = log['mapping'][k]
        cnt = log['counts_by_final'][final]
        lines.append(f"- {raw} -> {final} | {cnt}")
    lines.append("")
    lines.append("Detalle por turno:")
    lines.append("index\traw_label\tfinal_label\tcase\tcontent_found\tinterviewer\tstart_par\tend_par")
    for row in turn_logs:
        lines.append(f"{row['index']}\t{row['raw_label']}\t{row['final_label']}\t{row['kind']}\t{row['content_found']}\t{row['interviewer']}\t{row['start_par']}\t{row['end_par']}")
    return "\n".join(lines)

# =========================
# 5) Helpers de roles
# =========================
def normalize_role_label(label: str) -> str:
    s = re.sub(r'\s+', ' ', (label or '')).strip()
    if s.endswith(':'):
        s = s[:-1]
    return s.casefold()

def is_interviewer_final(label: str) -> bool:
    norm = normalize_role_label(label)
    return 'entrevistador' in norm

# =========================
# 🔥 Funciones Fireflies
# =========================
async def save_temp_file(upload_file: UploadFile) -> str:
    """Guarda el archivo de audio temporalmente"""
    file_id = str(uuid.uuid4())
    file_path = os.path.join(TEMP_DIR, f"{file_id}.mp3")
    
    async with aiofiles.open(file_path, "wb") as out_file:
        content = await upload_file.read()
        await out_file.write(content)
    
    return file_path

async def upload_audio_to_fireflies_via_url(audio_url: str, title: str) -> str:
    """Sube el audio a Fireflies usando URL"""
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

async def find_transcript_by_title(title: str):
    """Busca el transcript por título"""
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

async def get_transcript_text(transcript_id: str) -> str:
    """Obtiene el texto del transcript"""
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

def create_docx_from_transcript(transcript_text: str, title: str) -> io.BytesIO:
    """Crea un documento DOCX con la transcripción"""
    doc = Document()
    doc.add_heading(f"Transcripción: {title}", level=1)
    
    # Dividir el texto en párrafos y agregar con formato Speaker
    paragraphs = transcript_text.split('\n')
    current_speaker = None
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
            
        # Detectar si es un speaker (formato: "Speaker 1:", "Speaker 2:", etc.)
        speaker_match = re.match(r'^(speaker\s*\d+)\s*:?\s*(.*)$', para, re.IGNORECASE)
        if speaker_match:
            current_speaker = speaker_match.group(1)
            content = speaker_match.group(2)
            if content:
                p = doc.add_paragraph()
                write_label_plus_content(p, f"{current_speaker.upper()}: ", content, True, False)
        else:
            # Si no es speaker, agregar como párrafo normal
            if current_speaker:
                p = doc.add_paragraph()
                p.add_run(para)
            else:
                doc.add_paragraph(para)
    
    # Aplicar fuente global
    apply_global_font(doc, name=FONT_NAME, size_pt=FONT_SIZE_PT)
    
    # Guardar en memoria
    docx_stream = io.BytesIO()
    doc.save(docx_stream)
    docx_stream.seek(0)
    return docx_stream

# =========================
# 6) Procesamiento principal (versión API)
# =========================
def process_file_api(file_stream: io.BytesIO, interview_type: str, label_mapping_user: dict = None, file_name: str = "file.docx"):
    t0_total = time.perf_counter()
    doc = Document(file_stream)
    found_labels = OrderedDict()
    
    for p in doc.paragraphs:
        hit = is_label_start(p)
        if hit:
            _, raw_label, _ = hit
            k = normalize_label(raw_label)
            if k not in found_labels:
                found_labels[k] = raw_label.strip()
    
    # Lógica de mapeo corregida
    label_mapping = {}
    if label_mapping_user:
        for k, raw in found_labels.items():
            if k in label_mapping_user:
                final_label = ensure_colon_upper(label_mapping_user[k] or raw)
            else:
                final_label = ensure_colon_upper(raw)
            label_mapping[k] = final_label
    else:
        for k, raw in found_labels.items():
            label_mapping[k] = ensure_colon_upper(raw)
    
    t0_processing = time.perf_counter()
    i = 0
    changed = 0
    n = len(doc.paragraphs)
    time_only_count = sum(1 for p in doc.paragraphs if is_time_only(p))
    counts_by_final = defaultdict(int)
    turn_logs = []
    
    while i < n:
        p = doc.paragraphs[i]
        hit = is_label_start(p)
        if not hit:
            i += 1
            continue
        
        kind, raw_label, content_inline = hit
        key = normalize_label(raw_label)
        final_label = label_mapping.get(key)
        if not final_label:
            i += 1
            continue
        
        is_interviewer = is_interviewer_final(final_label)
        bold_label_flag = is_interviewer
        bold_content_flag = is_interviewer
        start_par = i
        content_found = False
        end_par = i
        
        if kind == 'inline':
            write_label_plus_content(p, final_label, content_inline, bold_label_flag, bold_content_flag, apply_spacing=True)
            content_found = True
            changed += 1
            counts_by_final[final_label] += 1
            turn_logs.append({
                'index': len(turn_logs) + 1,
                'raw_label': raw_label,
                'final_label': final_label,
                'kind': 'inline',
                'content_found': content_found,
                'interviewer': is_interviewer,
                'start_par': start_par,
                'end_par': end_par
            })
            i += 1
            continue
        
        j = i + 1
        while j < n:
            txtj = paragraph_text(doc.paragraphs[j]).strip()
            if not txtj or is_time_only(doc.paragraphs[j]):
                j += 1
                continue
            if is_label_start(doc.paragraphs[j]):
                break
            break
        
        if j >= n or is_label_start(doc.paragraphs[j]) or not paragraph_text(doc.paragraphs[j]).strip() or is_time_only(doc.paragraphs[j]):
            write_label_plus_content(p, final_label, "", bold_label_flag, bold_content_flag, apply_spacing=True)
            changed += 1
            counts_by_final[final_label] += 1
            turn_logs.append({
                'index': len(turn_logs) + 1,
                'raw_label': raw_label,
                'final_label': final_label,
                'kind': 'only',
                'content_found': False,
                'interviewer': is_interviewer,
                'start_par': start_par,
                'end_par': start_par
            })
            i += 1
            continue
        
        first_content = paragraph_text(doc.paragraphs[j])
        write_label_plus_content(p, final_label, first_content, bold_label_flag, bold_content_flag, apply_spacing=True)
        clear_paragraph(doc.paragraphs[j])
        content_found = True
        changed += 1
        counts_by_final[final_label] += 1
        
        k = j + 1
        last_non_time_idx = i
        while k < n and not is_label_start(doc.paragraphs[k]):
            if not is_time_only(doc.paragraphs[k]) and paragraph_text(doc.paragraphs[k]).strip():
                last_non_time_idx = k
            if is_interviewer:
                bold_whole_paragraph(doc.paragraphs[k])
            k += 1
        
        set_spacing(doc.paragraphs[last_non_time_idx], after_pt=SPACE_AFTER_LABEL_PT)
        end_par = last_non_time_idx
        
        turn_logs.append({
            'index': len(turn_logs) + 1,
            'raw_label': raw_label,
            'final_label': final_label,
            'kind': 'only+merge',
            'content_found': content_found,
            'interviewer': is_interviewer,
            'start_par': start_par,
            'end_par': end_par
        })
        i = k
    
    apply_global_font(doc, name=FONT_NAME, size_pt=FONT_SIZE_PT)
    
    t1_total = time.perf_counter()
    exec_total = t1_total - t0_total
    exec_processing = t1_total - t0_processing
    
    log = {
        'ts': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'input_file': file_name,
        'total_paragraphs': len(doc.paragraphs),
        'labels_detected': list(found_labels.values()),
        'mapping': label_mapping,
        'mapping_raw_order': list(found_labels.items()),
        'changed_count': changed,
        'time_only_count': time_only_count,
        'counts_by_final': counts_by_final,
        'exec_total_seconds': exec_total,
        'exec_total_hms': fmt_hms(exec_total),
        'exec_processing_seconds': exec_processing,
        'exec_processing_hms': fmt_hms(exec_processing),
    }
    
    docx_stream = io.BytesIO()
    doc.save(docx_stream)
    docx_stream.seek(0)
    
    txt_content = write_txt_control_report_in_memory(log, turn_logs)
    txt_stream = io.BytesIO(txt_content.encode('utf-8'))
    txt_stream.seek(0)
    
    return docx_stream, txt_stream

def detect_labels_api(file_stream: io.BytesIO):
    doc = Document(file_stream)
    found_labels = OrderedDict()
    
    for p in doc.paragraphs:
        hit = is_label_start(p)
        if hit:
            _, raw_label, _ = hit
            k = normalize_label(raw_label)
            if k not in found_labels:
                found_labels[k] = raw_label.strip()
    
    return list(found_labels.values())

# ==========================
# FastAPI
# ==========================
app = FastAPI(title="Formateador de Transcripciones con Fireflies")

ALLOWED_ORIGINS = [
    "https://www.dipli.ai",
    "https://dipli.ai",
    "https://isagarcivill09.wixsite.com/turop",
    "https://isagarcivill09.wixsite.com/turop/tienda",
    "https://isagarcivill09-wixsite-com.filesusr.com",
    "https://www.dipli.ai/preparaci%C3%B3n",
    "https://www-dipli-ai.filesusr.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montar archivos estáticos para servir archivos temporales
app.mount("/temp_audio", StaticFiles(directory="temp_audio"), name="temp_audio")

DOWNLOADS = {}
EXP_MINUTES = 5

def cleanup_downloads():
    now = datetime.utcnow()
    expired = [t for t, (_, exp) in DOWNLOADS.items() if exp <= now]
    for t in expired:
        DOWNLOADS.pop(t, None)

# ==========================
# 🔥 Nuevos endpoints Fireflies
# ==========================
@app.post("/fireflies/subir_audio/")
async def subir_audio_fireflies(
    file: UploadFile = File(...), 
    interview_type: str = Form(...),
    title: str = Form(...)
):
    """
    Sube audio a Fireflies y genera transcripción automáticamente.
    Retorna el documento DOCX con la transcripción para mapeo posterior.
    """
    try:
        # Validar archivo
        if not file.filename.lower().endswith(('.mp3', '.wav', '.m4a', '.ogg')):
            raise HTTPException(status_code=400, detail="El archivo debe ser de audio (mp3, wav, m4a, ogg)")
        
        # 1. Guardar temporalmente
        file_path = await save_temp_file(file)
        
        # 2. Crear URL pública para Render
        render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME')
        if not render_hostname:
            raise HTTPException(status_code=500, detail="Variable RENDER_EXTERNAL_HOSTNAME no configurada")
        
        public_url = f"https://{render_hostname}/temp_audio/{os.path.basename(file_path)}"
        print(f"URL pública generada: {public_url}")
        
        # 3. Subir a Fireflies
        upload_title = await upload_audio_to_fireflies_via_url(public_url, title)
        
        # 4. Polling para obtener transcript ID
        transcript_id = None
        for attempt in range(30):  # 30 intentos x 5s = 150s
            transcript_id = await find_transcript_by_title(upload_title)
            if transcript_id:
                break
            await asyncio.sleep(5)
        
        if not transcript_id:
            raise HTTPException(status_code=500, detail="No se encontró transcript en Fireflies")
        
        # 5. Polling hasta obtener texto
        transcript_text = None
        for attempt in range(30):
            transcript_text = await get_transcript_text(transcript_id)
            if transcript_text:
                break
            await asyncio.sleep(5)
        
        if not transcript_text:
            raise HTTPException(status_code=500, detail="El transcript no está listo")
        
        # 6. Crear DOCX con la transcripción
        docx_stream = create_docx_from_transcript(transcript_text, title)
        
        # 7. Guardar en descargas temporales
        token = str(uuid.uuid4())
        expiration = datetime.utcnow() + timedelta(minutes=EXP_MINUTES)
        DOWNLOADS[token] = (docx_stream, expiration)
        
        return JSONResponse(content={
            "token": token, 
            "filename": f"{title}_transcripcion.docx",
            "message": "Transcripción generada exitosamente. Use el token para descargar el archivo DOCX."
        })
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en el procesamiento: {e}")

@app.post("/fireflies/mapear_transcripcion/")
async def mapear_transcripcion_fireflies(
    token: str = Form(...),
    interview_type: str = Form(...),
    label_mapping: str = Form("null")
):
    """
    Aplica mapeo a una transcripción generada por Fireflies.
    Usa el token del endpoint anterior para obtener el DOCX.
    """
    try:
        cleanup_downloads()
        
        if token not in DOWNLOADS:
            raise HTTPException(status_code=404, detail="Token no válido o expirado")
        
        docx_stream, _ = DOWNLOADS.pop(token)
        docx_stream.seek(0)
        
        # Procesar mapeo
        mapping_data = None
        if label_mapping and label_mapping != "null":
            try:
                mapping_data = json.loads(label_mapping)
            except json.JSONDecodeError:
                print("Advertencia: El 'label_mapping' no es un JSON válido. Se usará el formato por defecto.")
        
        if mapping_data:
            mapping_data_normalized = {normalize_label(k): v for k, v in mapping_data.items()}
        else:
            mapping_data_normalized = None
        
        # Aplicar mapeo
        processed_docx_stream, txt_stream = process_file_api(
            file_stream=docx_stream,
            interview_type=interview_type,
            label_mapping_user=mapping_data_normalized,
            file_name="transcripcion_fireflies.docx"
        )
        
        # Crear ZIP final
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            zip_file.writestr("transcripcion_mapeada_FINAL.docx", processed_docx_stream.getvalue())
            zip_file.writestr("registro_control_proceso.txt", txt_stream.getvalue())
        
        zip_buffer.seek(0)
        
        # Guardar resultado final
        final_token = str(uuid.uuid4())
        expiration = datetime.utcnow() + timedelta(minutes=EXP_MINUTES)
        DOWNLOADS[final_token] = (zip_buffer, expiration)
        
        return JSONResponse(content={
            "token": final_token, 
            "filename": "transcripcion_mapeada_FINAL.zip",
            "message": "Transcripción mapeada exitosamente."
        })
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en el mapeo: {e}")

# ==========================
# Endpoints existentes (mantenidos)
# ==========================
@app.post("/detectar_etiquetas/")
async def detectar_etiquetas(file: UploadFile = File(...)):
    """Detecta etiquetas "Speaker N" en un documento .docx para el mapeo."""
    try:
        file_content = await file.read()
        file_stream = io.BytesIO(file_content)
        labels = detect_labels_api(file_stream)
        
        if not labels:
            return JSONResponse(content={"labels": [], "message": "No se detectaron etiquetas 'Speaker N'."})
        
        return JSONResponse(content={"labels": labels})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al detectar etiquetas: {e}")

@app.post("/formatear/")
async def formatear_transcripcion(
    file: UploadFile = File(...),
    interview_type: str = Form(...),
    label_mapping: str = Form("null")
):
    """Formatea un documento .docx y genera un archivo de registro."""
    try:
        if not file.filename.endswith('.docx'):
            raise HTTPException(status_code=400, detail="El archivo debe ser un .docx")
        
        file_content = await file.read()
        file_stream = io.BytesIO(file_content)
        
        mapping_data = None
        if label_mapping and label_mapping != "null":
            try:
                mapping_data = json.loads(label_mapping)
            except json.JSONDecodeError:
                print("Advertencia: El 'label_mapping' no es un JSON válido. Se usará el formato por defecto.")
        
        if mapping_data:
            mapping_data_normalized = {normalize_label(k): v for k, v in mapping_data.items()}
        else:
            mapping_data_normalized = None
        
        docx_stream, txt_stream = process_file_api(
            file_stream=file_stream,
            interview_type=interview_type,
            label_mapping_user=mapping_data_normalized,
            file_name=file.filename
        )
        
        base_filename = Path(file.filename).stem
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            docx_name = f"{base_filename}_formateado_FINAL.docx"
            zip_file.writestr(docx_name, docx_stream.getvalue())
            txt_name = f"{base_filename}_registro_control_proceso.txt"
            zip_file.writestr(txt_name, txt_stream.getvalue())
        
        zip_buffer.seek(0)
        
        token = str(uuid.uuid4())
        expiration = datetime.utcnow() + timedelta(minutes=EXP_MINUTES)
        DOWNLOADS[token] = (zip_buffer, expiration)
        
        return JSONResponse(content={"token": token, "filename": f"{base_filename}_formateado.zip"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en el procesamiento: {e}")

@app.get("/descargar/{token}")
async def descargar_archivo(token: str):
    """Permite la descarga de un archivo comprimido por un token."""
    cleanup_downloads()
    
    if token not in DOWNLOADS:
        raise HTTPException(status_code=404, detail="Token de descarga no válido o expirado.")
    
    zip_buffer, _ = DOWNLOADS.pop(token)
    
    response = StreamingResponse(
        io.BytesIO(zip_buffer.getvalue()),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=archivos_formateados.zip",
            "Content-Length": str(len(zip_buffer.getvalue()))
        }
    )
    return response

# ==========================
# Endpoint de salud
# ==========================
@app.get("/")
async def root():
    return {"message": "API de Formateador de Transcripciones con Fireflies funcionando correctamente"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "fireflies_configured": bool(FIREFLIES_API_KEY)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


