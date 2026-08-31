"""
Darslik Studiyasi - Bulutli server (persistent job tizimi)

Ishga tushirish (lokal sinov uchun):
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000

Railway'ga joylashtirish uchun README.txt'ga qarang.
"""
import asyncio
import httpx
import json
import re
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import database as db
import keys_manager
import transcription
import translation
import glossary_data
import tts
import worker
from storage import (VIDEOS_DIR, RESULTS_DIR, UPLOADS_DIR, CHUNKS_DIR, SPLIT_DIR, CLOUD_DIR, MAX_UPLOAD_SIZE, ADMIN_TOKEN,
                      UPLOAD_CHUNK_SIZE, CHUNK_SECONDS, MAX_WHISPER_CONCURRENCY, MAX_ACTIVE_VIDEO_JOBS,
                      MAX_ACTIVE_TTS_JOBS, REPETITION_THRESHOLD, DARSLIK_API_KEY, IDEA_FLOW_URL,
                      TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, LOCAL_BOT_API_URL, INBOUND_BOT_TOKEN,
                      safe_name, disk_usage, has_space_for)

BASE = Path(__file__).resolve().parent

RANGE_CHUNK_SIZE = 1024 * 1024  # 1 MB


def range_file_response(request: Request, path: Path, media_type: str):
    """Video/audio surish (seek) ishlashi uchun HTTP Range so'rovlarini qo'lda
    qo'llab-quvvatlaydi (o'rnatilgan FileResponse buni har doim ham qilavermaydi)."""
    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    if not range_header:
        def iter_whole():
            with path.open("rb") as f:
                while True:
                    chunk = f.read(RANGE_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        return StreamingResponse(iter_whole(), media_type=media_type, headers={
            "Accept-Ranges": "bytes", "Content-Length": str(file_size),
        })

    try:
        range_value = range_header.strip().split("=")[1]
        start_str, end_str = range_value.split("-")
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        end = min(end, file_size - 1)
    except (IndexError, ValueError):
        start, end = 0, file_size - 1

    if start >= file_size or start > end:
        raise HTTPException(416, "Range noto'g'ri.", headers={"Content-Range": f"bytes */{file_size}"})

    chunk_length = end - start + 1

    def iter_range():
        with path.open("rb") as f:
            f.seek(start)
            remaining = chunk_length
            while remaining > 0:
                chunk = f.read(min(RANGE_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(iter_range(), status_code=206, media_type=media_type, headers={
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(chunk_length),
    })


app = FastAPI(title="Darslik Studiyasi - Cloud")

# Diqqat: production uchun bu yerga faqat o'zingizning sayt manzilingizni yozing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    db.init_db()
    await worker.recover_and_start()
    if INBOUND_BOT_TOKEN:
        import telegram_bot
        asyncio.create_task(telegram_bot.poll_updates())


def check_admin(request: Request):
    if not ADMIN_TOKEN:
        return
    token = request.headers.get("X-Admin-Token", "")
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "Admin token noto'g'ri.")


@app.get("/api/debug/telegram-file-test")
async def debug_telegram_file_test(file_id: str = None, file_path: str = None, _=Depends(check_admin)):
    """VAQTINCHA diagnostika endpointi - self-hosted telegram-bot-api serverining
    /file/ fayl xizmatini darslikservet konteyneri ichidan to'g'ridan-to'g'ri sinash
    uchun (muammo hal bo'lgach olib tashlanadi)."""
    from storage import INBOUND_BOT_TOKEN

    def _mask(url: str) -> str:
        return url.replace(INBOUND_BOT_TOKEN, "***MASKED***") if INBOUND_BOT_TOKEN else url

    results = {}
    async with httpx.AsyncClient(timeout=30) as client:
        base = f"{LOCAL_BOT_API_URL.rstrip('/')}/bot{INBOUND_BOT_TOKEN}"
        if not file_path and file_id:
            r = await client.post(f"{base}/getFile", data={"file_id": file_id})
            results["getFile"] = {"status": r.status_code, "body": r.text[:1000]}
            try:
                file_path = r.json()["result"]["file_path"]
            except Exception:
                file_path = None
        if file_path:
            file_url = f"{LOCAL_BOT_API_URL.rstrip('/')}/file/bot{INBOUND_BOT_TOKEN}/{file_path}"
            r2 = await client.get(file_url)
            results["file_download"] = {
                "url": _mask(file_url),
                "status": r2.status_code,
                "headers": dict(r2.headers),
                "body_preview": r2.text[:1000] if r2.status_code != 200 else f"<{len(r2.content)} bytes OK>",
            }
        else:
            results["note"] = "file_id yoki file_path query parametri kerak (masalan ?file_id=... yoki ?file_path=videos/file_0.mp4)"
    return results


# ---------------------------------------------------------------------------
#                          UMUMIY
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    import transcription
    return {"ok": True, "ffmpeg": bool(transcription.ffmpeg_exe())}


@app.get("/api/storage")
async def api_storage():
    return disk_usage()


@app.get("/api/config")
async def api_config():
    return {
        "chunk_seconds": CHUNK_SECONDS,
        "max_whisper_concurrency": MAX_WHISPER_CONCURRENCY,
        "max_active_video_jobs": MAX_ACTIVE_VIDEO_JOBS,
        "max_active_tts_jobs": MAX_ACTIVE_TTS_JOBS,
        "max_upload_size": MAX_UPLOAD_SIZE,
        "repetition_threshold": REPETITION_THRESHOLD,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    p = BASE / "index.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Darslik Studiyasi server ishlamoqda.</h1>")


# ---------------------------------------------------------------------------
#            PWA (planshet/telefon ekraniga "ilova" sifatida o'rnatish)
# ---------------------------------------------------------------------------

if (BASE / "static" / "icons").exists():
    app.mount("/icons", StaticFiles(directory=BASE / "static" / "icons"), name="icons")


@app.get("/manifest.json")
async def pwa_manifest():
    return FileResponse(BASE / "static" / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_service_worker():
    # Root darajasida joylashishi shart - shunda service worker butun saytni
    # (barcha /api/... yo'llarini ham) qamrab oladi, /static/sw.js bo'lganida
    # faqat /static/ ostidagi manzillarni "eshita" olar edi.
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript")


# ---------------------------------------------------------------------------
#                          VIDEO KUTUBXONASI
# ---------------------------------------------------------------------------

def video_public(v: dict) -> dict:
    return {
        "id": v["id"], "original_name": v["original_name"], "file_size": v["file_size"],
        "duration": v["duration"], "status": v["status"], "blocked_reason": v["blocked_reason"],
        "chunk_count": v["chunk_count"],
        "language": v["language"], "instruction": v["instruction"], "topic_group": v["topic_group"],
        "folder_id": v["folder_id"], "idea_flow_sent_at": v["idea_flow_sent_at"],
        "telegram_send_status": v["telegram_send_status"] or "none", "telegram_send_error": v["telegram_send_error"],
        "detected_language": v["detected_language"], "progress": v["progress"],
        "message": v["message"], "error": v["error"],
        "repetition_chunk_index": v["repetition_chunk_index"], "repetition_info": v["repetition_info"],
        "transcript_approved": bool(v["transcript_approved"]),
        "translation_status": v["translation_status"], "translation_source": v["translation_source"],
        "audio_status": v["audio_status"], "final_video_status": v["final_video_status"],
        "cost_total": v["cost_total"] or 0,
        "has_thumbnail": bool(v["thumbnail_path"]),
        "flagged_issues_count": len(json.loads(v["flagged_issues"])) if v["flagged_issues"] else 0,
        "created_at": v["created_at"], "updated_at": v["updated_at"],
    }


def chunk_detail(c: dict, transcript_segments: list = None) -> dict:
    text = ""
    issues = []
    if c["transcript"]:
        payload = json.loads(c["transcript"])
        text = " ".join(s["text"] for s in payload.get("segments", []))
        issues = payload.get("issues", [])
    running_since = worker.CHUNK_STARTED_AT.get(c["id"])
    # Shu bo'lak ichiga tushadigan aniq segmentlar (video-darajasidagi transcript_segments'dan,
    # global indeks bilan) - foydalanuvchi har bir jumlani alohida tahrirlashi/tinglashi/qayta
    # yuborishi uchun (glossary-tuzatilgan yakuniy matn bilan, chunk-lokal xom matn bilan emas).
    segments = []
    for i, s in enumerate(transcript_segments or []):
        if s["start"] >= c["start_time"] - 0.5 and s["start"] < c["end_time"] + 0.5:
            segments.append({"index": i, "start": s["start"], "end": s["end"], "text": s["text"]})
    return {
        "id": c["id"], "chunk_index": c["chunk_index"], "start_time": c["start_time"],
        "end_time": c["end_time"], "duration": round(c["end_time"] - c["start_time"], 1),
        "status": c["status"], "attempts": c["attempts"], "error": c["error"],
        "text": text, "issues": issues,
        "running_seconds": round(time.time() - running_since, 0) if running_since else None,
        "segments": segments,
    }


@app.get("/api/videos")
async def list_videos():
    rows = db.fetchall("SELECT * FROM videos WHERE kind = 'pipeline' OR kind IS NULL ORDER BY created_at DESC")
    return [video_public(r) for r in rows]


def split_video_public(v: dict) -> dict:
    return {
        "id": v["id"], "original_name": v["original_name"], "file_size": v["file_size"],
        "duration": v["duration"], "status": v["status"],
        "telegram_send_status": v["telegram_send_status"] or "none", "telegram_send_error": v["telegram_send_error"],
        "split_total_parts": v["split_total_parts"] or 0, "split_parts_sent": v["split_parts_sent"] or 0,
        "idea_flow_sent_at": v["idea_flow_sent_at"],
        "has_thumbnail": bool(v["thumbnail_path"]),
        "created_at": v["created_at"], "updated_at": v["updated_at"],
    }


@app.get("/api/split-videos")
async def list_split_videos():
    rows = db.fetchall("SELECT * FROM videos WHERE kind = 'split_only' ORDER BY created_at DESC")
    return [split_video_public(r) for r in rows]


# ---------------------------------------------------------------------------
#                          PAPKALAR
# ---------------------------------------------------------------------------

@app.get("/api/folders")
async def list_folders():
    folders = db.fetchall("SELECT * FROM folders ORDER BY name COLLATE NOCASE ASC")
    counts = db.fetchall("SELECT folder_id, COUNT(*) as n FROM videos WHERE folder_id IS NOT NULL GROUP BY folder_id")
    count_by_id = {c["folder_id"]: c["n"] for c in counts}
    return [{"id": f["id"], "name": f["name"], "created_at": f["created_at"],
             "video_count": count_by_id.get(f["id"], 0)} for f in folders]


@app.post("/api/folders")
async def create_folder(name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Papka nomi bo'sh bo'lishi mumkin emas.")
    folder_id = db.new_id()
    db.execute("INSERT INTO folders (id, name, created_at) VALUES (?, ?, ?)", (folder_id, name, db.now()))
    return {"id": folder_id, "name": name}


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str):
    f = db.fetchone("SELECT id FROM folders WHERE id = ?", (folder_id,))
    if not f:
        raise HTTPException(404, "Papka topilmadi.")
    db.execute("UPDATE videos SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
    db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    return {"ok": True}


@app.post("/api/videos/{video_id}/folder")
async def set_video_folder(video_id: str, folder_id: str = Form("")):
    v = db.fetchone("SELECT id FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    folder_id = folder_id.strip() or None
    if folder_id:
        f = db.fetchone("SELECT id FROM folders WHERE id = ?", (folder_id,))
        if not f:
            raise HTTPException(404, "Papka topilmadi.")
    db.execute("UPDATE videos SET folder_id = ? WHERE id = ?", (folder_id, video_id))
    return {"ok": True}


@app.get("/api/videos/{video_id}")
async def get_video(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    chunks = db.fetchall(
        "SELECT id, chunk_index, start_time, end_time, status, attempts, error, transcript FROM chunks "
        "WHERE video_id = ? ORDER BY chunk_index ASC", (video_id,))
    logs = db.get_logs(video_id, 200)
    results = db.fetchall("SELECT id, kind, filename, created_at FROM results WHERE video_id = ?", (video_id,))
    out = video_public(v)
    parsed_transcript_segments = _json_or_empty(v["transcript_segments"])
    out["chunks"] = [chunk_detail(c, parsed_transcript_segments) for c in chunks]
    out["logs"] = logs
    out["results"] = results
    out["transcript_text"] = v["transcript_text"] or ""
    out["translation_text"] = v["translation_text"] or ""
    out["expected_segment_count"] = len(chunks) if v["transcript_segments"] else None
    out["flagged_issues"] = json.loads(v["flagged_issues"]) if v["flagged_issues"] else []
    if v["tts_job_id"]:
        tj = db.fetchone("SELECT status, error, total_segments, completed_segments FROM tts_jobs WHERE id = ?",
                          (v["tts_job_id"],))
        out["tts_job"] = tj
    return out


@app.get("/api/videos/{video_id}/thumbnail")
async def get_thumbnail(video_id: str):
    v = db.fetchone("SELECT thumbnail_path FROM videos WHERE id = ?", (video_id,))
    if not v or not v["thumbnail_path"] or not Path(v["thumbnail_path"]).exists():
        raise HTTPException(404, "Thumbnail topilmadi.")
    return FileResponse(v["thumbnail_path"], media_type="image/jpeg")


@app.delete("/api/videos/{video_id}")
async def delete_video(video_id: str, mode: str = "full", _=Depends(check_admin)):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if mode == "results":
        db.execute("DELETE FROM results WHERE video_id = ?", (video_id,))
        shutil.rmtree(RESULTS_DIR / video_id, ignore_errors=True)
    elif mode == "chunks":
        db.execute("UPDATE chunks SET status='pending', transcript=NULL WHERE video_id = ?", (video_id,))
        worker._update_video(video_id, status="segments_ready", blocked_reason=None)
    else:
        worker.CANCEL_FLAGS[video_id] = True
        if v["tts_job_id"]:
            db.execute("DELETE FROM tts_segments WHERE job_id = ?", (v["tts_job_id"],))
            db.execute("DELETE FROM tts_jobs WHERE id = ?", (v["tts_job_id"],))
        db.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
        db.execute("DELETE FROM results WHERE video_id = ?", (video_id,))
        db.execute("DELETE FROM job_logs WHERE video_id = ?", (video_id,))
        db.execute("DELETE FROM costs WHERE video_id = ?", (video_id,))
        db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        shutil.rmtree(VIDEOS_DIR / video_id, ignore_errors=True)
        shutil.rmtree(CHUNKS_DIR / video_id, ignore_errors=True)
        shutil.rmtree(RESULTS_DIR / video_id, ignore_errors=True)
        from storage import TTS_DIR
        if v["tts_job_id"]:
            shutil.rmtree(TTS_DIR / v["tts_job_id"], ignore_errors=True)
    return {"ok": True}


@app.post("/api/videos/{video_id}/segment")
async def segment_video_endpoint(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] not in ("uploaded", "segmenting", "segments_ready"):
        raise HTTPException(400, f"Video holati '{v['status']}' - bo'laklarga bo'lish mumkin emas.")
    worker.enqueue_segment(video_id)
    return {"ok": True}


@app.post("/api/videos/{video_id}/transcribe")
async def transcribe_endpoint(video_id: str, language: str = Form(""), instruction: str = Form(""),
                                topic_group: str = Form("")):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] not in ("segments_ready", "transcription_ready"):
        raise HTTPException(400, f"Video holati '{v['status']}' - transkripsiyani boshlab bo'lmaydi. "
                                  f"Avval videoni bo'laklarga bo'ling.")
    if not keys_manager.has_any_active_key():
        raise HTTPException(400, "Ishlaydigan OpenAI API kalit topilmadi. Avval API kalit qo'shing.")
    db.execute("UPDATE chunks SET status = 'pending', transcript = NULL WHERE video_id = ?", (video_id,))
    worker.start_transcription(video_id, language, instruction, topic_group)
    return {"ok": True}


@app.get("/api/glossary/groups")
async def glossary_groups_endpoint():
    return {"groups": glossary_data.GLOSSARY_GROUPS}


@app.get("/api/videos/{video_id}/transcript/blocks")
async def transcript_blocks_endpoint(video_id: str):
    """Original (Whisper) matnni bo'lak-darajasida (vaqt belgisi + matn) qaytaradi -
    'Video → Matn' bosqichida qo'lda tahrirlash uchun."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    segments = _json_or_empty(v["transcript_segments"])
    return [{"index": i, "start": s["start"], "end": s["end"], "text": s["text"]} for i, s in enumerate(segments)]


@app.get("/api/videos/{video_id}/transcript/segments/{index}/audio")
async def segment_audio_endpoint(video_id: str, index: int):
    """Bitta aniq segmentning original videodagi audiosini qaytaradi - foydalanuvchi
    Whisper nega xato yozganini tushunish uchun o'sha joyni to'g'ridan-to'g'ri tinglashi mumkin."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["path"] or not Path(v["path"]).exists():
        raise HTTPException(404, "Original video fayli topilmadi.")
    segments = _json_or_empty(v["transcript_segments"])
    if index < 0 or index >= len(segments):
        raise HTTPException(404, "Bunday segment mavjud emas.")
    seg = segments[index]
    clip_dir = CHUNKS_DIR / video_id / "listen"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / f"seg_{index:05d}.mp3"
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None, transcription.extract_audio_slice, Path(v["path"]), seg["start"], seg["end"], clip_path)
    except Exception as e:
        raise HTTPException(500, f"Audio ajratishda xato: {e}")
    return FileResponse(clip_path, media_type="audio/mpeg")


@app.post("/api/videos/{video_id}/transcript/segments/{index}/retranscribe")
async def retranscribe_segment_endpoint(video_id: str, index: int):
    """Bitta aniq segmentni original videodan qayta ajratib, qayta Whisper'ga yuboradi -
    butun bo'lakni emas, faqat shu bitta segmentni. Mos tarjima bo'lagi ham tozalanadi
    (qayta tarjima qilinishi kerakligini bildirish uchun)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    try:
        result = await worker.retranscribe_segment(video_id, index)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result}


@app.post("/api/videos/{video_id}/transcript/segments/{index}/text")
async def save_segment_text_endpoint(video_id: str, index: int, text: str = Form("")):
    """Bitta segment matnini to'g'ridan-to'g'ri qo'lda tuzatib saqlaydi (Whisper'ga
    yuborilmaydi) - '5 daqiqalik bo'lak' ko'rinishida har bir jumlani joyida tez
    tahrirlash uchun (butun ro'yxatni /save-blocks orqali yubormasdan)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    segments = _json_or_empty(v["transcript_segments"])
    if index < 0 or index >= len(segments):
        raise HTTPException(404, "Bunday segment mavjud emas.")
    segments[index] = {"start": segments[index]["start"], "end": segments[index]["end"], "text": text.strip()}
    txt_text = transcription.build_txt(segments)
    worker._update_video(video_id, transcript_text=txt_text, transcript_segments=json.dumps(segments, ensure_ascii=False))
    worker.write_transcript_results(video_id)
    db.log_line(video_id, f"{index + 1}-segment matni qo'lda tahrirlandi (bo'lak ko'rinishidan).")
    return {"ok": True}


@app.post("/api/videos/{video_id}/transcript/save-blocks")
async def transcript_save_blocks_endpoint(video_id: str, payload: dict):
    """Original matnning istalgan bo'lagini qo'lda tuzatib saqlaydi (Whisper'ni
    qayta chaqirmasdan) - foydalanuvchi xato deb hisoblagan joyni to'g'ridan-to'g'ri o'zgartiradi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    texts = payload.get("texts")
    if not isinstance(texts, list):
        raise HTTPException(400, "'texts' massiv bo'lishi kerak.")
    try:
        result = worker.apply_transcript_edits(video_id, texts)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result}


@app.post("/api/videos/{video_id}/approve")
async def approve_transcript_endpoint(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] != "transcription_ready":
        raise HTTPException(400, "Faqat transkripsiya tayyor bo'lganda tasdiqlash mumkin.")
    worker.approve_transcript(video_id)
    return {"ok": True}


@app.post("/api/videos/{video_id}/translate")
async def translate_auto_endpoint(video_id: str, provider: str = Form("openai")):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    if not keys_manager.has_any_active_key(provider=provider):
        provider_label = "Claude" if provider == "claude" else "OpenAI"
        raise HTTPException(400, f"Ishlaydigan {provider_label} API kalit topilmadi. Avval API kalit qo'shing.")
    worker._update_video(video_id, translation_status="generating", message="Avtomatik tarjima qilinmoqda...")
    asyncio.create_task(worker.run_auto_translate(video_id, provider))
    return {"ok": True}


@app.post("/api/videos/{video_id}/translate/fill-empty")
async def translate_fill_empty_endpoint(video_id: str, provider: str = Form("openai")):
    """Faqat matni bo'sh qolgan tarjima bo'laklarini AI orqali to'ldiradi
    (masalan, to'g'ridan-to'g'ri SRT original bo'laklar sonini to'liq qamrab
    olmagan bo'lsa)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    segments = _json_or_empty(v["translation_segments"])
    if not segments:
        raise HTTPException(400, "Tarjima segmentlari topilmadi.")
    empty_count = sum(1 for s in segments if not (s.get("text") or "").strip())
    if empty_count == 0:
        return {"ok": True, "empty_count": 0}
    if not keys_manager.has_any_active_key(provider=provider):
        provider_label = "Claude" if provider == "claude" else "OpenAI"
        raise HTTPException(400, f"Ishlaydigan {provider_label} API kalit topilmadi. Avval API kalit qo'shing.")
    worker._update_video(video_id, message=f"{empty_count} ta bo'sh bo'lak tarjima qilinmoqda...")
    asyncio.create_task(worker.fill_empty_translations(video_id, provider))
    return {"ok": True, "empty_count": empty_count}


@app.post("/api/videos/{video_id}/translate/text")
async def translate_manual_text_endpoint(video_id: str, text: str = Form(...)):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    segments = _json_or_empty(v["transcript_segments"])
    try:
        texts = translation.parse_manual_translation(text, segments)
    except ValueError as e:
        raise HTTPException(400, str(e))
    worker.apply_manual_translation(video_id, texts, "pasted")
    return {"ok": True}


@app.post("/api/videos/{video_id}/translate/file")
async def translate_manual_file_endpoint(video_id: str, file: UploadFile = File(...)):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    raw = await file.read()
    name = (file.filename or "").lower()
    if name.endswith(".docx"):
        text = _extract_docx_text(raw)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="ignore")
    segments = _json_or_empty(v["transcript_segments"])
    try:
        texts = translation.parse_manual_translation(text, segments)
    except ValueError as e:
        raise HTTPException(400, str(e))
    worker.apply_manual_translation(video_id, texts, "uploaded")
    return {"ok": True}


@app.post("/api/videos/{video_id}/translate/srt-direct")
async def translate_srt_direct_endpoint(video_id: str, file: UploadFile = File(...)):
    """Tayyor o'zbekcha SRT faylni o'z vaqt belgilari bilan to'g'ridan-to'g'ri yuklaydi -
    original transkripsiya bo'laklari soniga bog'liq emas."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    raw = await file.read()
    name = (file.filename or "").lower()
    if not name.endswith(".srt"):
        raise HTTPException(400, "Faqat .srt fayl qabul qilinadi.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="ignore")
    try:
        segments = translation.parse_srt_direct(text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    worker.apply_direct_srt_translation(video_id, segments)
    return {"ok": True, "segment_count": len(segments)}


@app.post("/api/videos/{video_id}/translate/srt-direct-from-cloud")
async def translate_srt_direct_from_cloud_endpoint(video_id: str, cloud_file_id: str = Form(...)):
    """translate/srt-direct bilan bir xil, faqat fayl kompyuterdan emas,
    Bulutdagi (cloud_files) fayldan olinadi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND kind = 'file'", (cloud_file_id,))
    if not f or not Path(f["path"]).exists():
        raise HTTPException(404, "Bulutda bunday fayl topilmadi.")
    name = (f["original_name"] or "").lower()
    if not name.endswith(".srt"):
        raise HTTPException(400, "Faqat .srt fayl qabul qilinadi.")
    raw = Path(f["path"]).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="ignore")
    try:
        segments = translation.parse_srt_direct(text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    worker.apply_direct_srt_translation(video_id, segments)
    return {"ok": True, "segment_count": len(segments)}


def _json_or_empty(raw):
    import json
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _extract_docx_text(raw: bytes) -> str:
    import io
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml_content = z.read("word/document.xml")
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root = ET.fromstring(xml_content)
        paragraphs = []
        for p in root.iter(f"{{{ns['w']}}}p"):
            texts = [node.text or "" for node in p.iter(f"{{{ns['w']}}}t")]
            paragraphs.append("".join(texts))
        return "\n\n".join(paragraphs)
    except Exception as e:
        raise HTTPException(400, f".docx faylni o'qib bo'lmadi: {e}")


@app.get("/api/videos/{video_id}/translation/blocks")
async def translation_blocks_endpoint(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    return worker.get_translation_blocks(video_id)


@app.post("/api/videos/{video_id}/translation/preview-file")
async def translation_preview_file_endpoint(video_id: str, file: UploadFile = File(...)):
    """Yangi tarjima faylini yuklab, matnlarni ko'rish uchun ajratib beradi
    (hali saqlanmaydi - foydalanuvchi qaysi bo'laklarni almashtirishni tanlaydi)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    raw = await file.read()
    name = (file.filename or "").lower()
    if name.endswith(".docx"):
        text = _extract_docx_text(raw)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="ignore")
    segments = _json_or_empty(v["transcript_segments"])
    try:
        texts = translation.parse_manual_translation(text, segments)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"texts": texts}


@app.post("/api/videos/{video_id}/translation/save-blocks")
async def translation_save_blocks_endpoint(video_id: str, payload: dict):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    texts = payload.get("texts")
    if not isinstance(texts, list):
        raise HTTPException(400, "'texts' massiv bo'lishi kerak.")
    try:
        result = worker.apply_block_edits(video_id, texts)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result}


@app.post("/api/videos/{video_id}/translation/fix-segments")
async def translation_fix_segments_endpoint(video_id: str, indices: str = Form(...),
                                              file: UploadFile = File(...)):
    """'Xatoni to'g'irlash': foydalanuvchi ko'rsatgan segment raqamlari uchun,
    yangi yuklangan SRT'dan vaqt belgisi orqali mos bo'lakni oladi. Vaqt mos
    kelmasa aniq xato qaytaradi va HECH NARSA o'zgartirmaydi (hammasi yoki
    hech narsa - qisman noto'g'ri natija saqlanmasligi uchun)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    original_segments = _json_or_empty(v["transcript_segments"])
    if not original_segments:
        raise HTTPException(400, "Original matn segmentlari topilmadi.")
    current_translations = _json_or_empty(v["translation_segments"])
    if not current_translations or len(current_translations) != len(original_segments):
        raise HTTPException(400, "Avval tarjima tayyor bo'lishi kerak (segmentlar soni original bilan mos bo'lishi shart).")

    try:
        target_indices = sorted(set(int(x.strip()) - 1 for x in indices.split(",") if x.strip()))
    except ValueError:
        raise HTTPException(400, "Segment raqamlarini vergul bilan ajratib kiriting (masalan: 3, 15, 42).")
    if not target_indices:
        raise HTTPException(400, "Kamida bitta segment raqami kiriting.")

    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("cp1251", errors="ignore")

    try:
        matched, errors = translation.match_segments_by_timestamp(content, target_indices, original_segments)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if errors:
        # Hammasi yoki hech narsa: bironta segment topilmasa, hech narsa o'zgartirilmaydi
        raise HTTPException(400, {
            "message": f"{len(errors)} ta segment vaqt belgisi mos kelmadi - hech narsa o'zgartirilmadi.",
            "errors": errors,
        })

    new_texts = [t["text"] if isinstance(t, dict) else t for t in current_translations]
    for idx, text in matched.items():
        new_texts[idx] = text

    try:
        result = worker.apply_block_edits(video_id, new_texts)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "fixed_indices": [i + 1 for i in matched.keys()], **result}


@app.post("/api/videos/{video_id}/audio")
async def create_audio_endpoint(
    video_id: str,
    provider: str = Form(...),
    voice: str = Form(""),
    mood: str = Form(""),
    speed: float = Form(1.0),
    instructions: str = Form(""),
    aisha_key: str = Form(""),
    stretch_to_fit: bool = Form(True),
    skip_empty: bool = Form(False),
):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] not in ("translation_ready", "audio_processing", "audio_ready"):
        raise HTTPException(400, "Avval o'zbekcha tarjimani tayyorlang.")
    segments = _json_or_empty(v["translation_segments"])
    if not segments:
        raise HTTPException(400, "Tarjima segmentlari topilmadi.")
    empty_indices = [i for i, s in enumerate(segments) if not (s.get("text") or "").strip()]
    if empty_indices and not skip_empty:
        raise HTTPException(409, {"kind": "empty_segments", "count": len(empty_indices),
                                   "indices": [i + 1 for i in empty_indices[:20]]})
    if provider == "aisha" and not aisha_key.strip():
        raise HTTPException(400, "Aisha API kalit kiritilmagan.")
    if provider == "openai" and not keys_manager.has_any_active_key():
        raise HTTPException(400, "Ishlaydigan OpenAI API kalit topilmadi. Avval API kalit qo'shing.")

    job_id = tts.create_job(v["original_name"], provider, segments, voice, mood, speed, instructions,
                             aisha_key.strip(), stretch_to_fit, video_id=video_id)
    worker._update_video(video_id, status="audio_processing", blocked_reason=None,
                          audio_status="generating", tts_job_id=job_id, message="Audio yaratilmoqda...")
    return {"ok": True, "tts_job_id": job_id}


@app.post("/api/videos/{video_id}/render")
async def render_endpoint(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] not in ("audio_ready", "completed") or not v["audio_path"]:
        raise HTTPException(400, "Avval audio tayyor bo'lishi kerak.")
    worker.enqueue_render(video_id)
    return {"ok": True}


async def _send_video_file_to_telegram(video_id: str, video_path: str, title: str):
    """Videoning o'zini (havola emas) mahalliy Bot API server orqali Telegram'ga
    yuboradi - bu 2 GB gacha ruxsat beradi (oddiy api.telegram.org 50 MB bilan
    cheklaydi). 1.9 GB'dan katta bo'lsa avval qismlarga bo'linadi. Uzoq davom
    etishi mumkin, shuning uchun background'da ishlaydi."""
    split_dir = SPLIT_DIR / video_id
    try:
        loop = asyncio.get_event_loop()
        parts = await loop.run_in_executor(
            None, transcription.split_video_by_size, Path(video_path), split_dir)
        total = len(parts)
        db.execute("UPDATE videos SET split_total_parts = ?, split_parts_sent = 0 WHERE id = ?", (total, video_id))

        async with httpx.AsyncClient(timeout=1200) as client:
            for i, part in enumerate(parts, start=1):
                caption = title if total == 1 else f"{title}\n\nQism {i}/{total}"
                filename = Path(part).name if total == 1 else f"{safe_name(Path(title).stem)}_{i:02d}.mp4"
                with open(part, "rb") as f:
                    resp = await client.post(
                        f"{LOCAL_BOT_API_URL.rstrip('/')}/bot{TELEGRAM_BOT_TOKEN}/sendVideo",
                        data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "supports_streaming": "true"},
                        files={"video": (filename, f, "video/mp4")},
                    )
                if resp.status_code >= 400:
                    raise RuntimeError(f"Telegram xatosi ({resp.status_code}, qism {i}/{total}): {resp.text[:400]}")
                db.execute("UPDATE videos SET split_parts_sent = ? WHERE id = ?", (i, video_id))

        db.execute("UPDATE videos SET telegram_send_status = 'sent', telegram_send_error = NULL WHERE id = ?",
                   (video_id,))
        db.log_line(video_id, f"Video Telegram botga muvaffaqiyatli yuborildi ({total} qism).")
    except Exception as e:
        db.execute("UPDATE videos SET telegram_send_status = 'error', telegram_send_error = ? WHERE id = ?",
                   (str(e)[:500], video_id))
        db.log_line(video_id, f"Telegram botga yuborishda xato: {e}")
    finally:
        if split_dir.exists():
            shutil.rmtree(split_dir, ignore_errors=True)


@app.post("/api/videos/{video_id}/send-to-bot")
async def send_to_bot_endpoint(video_id: str, request: Request):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] != "completed" or not v["final_video_path"]:
        raise HTTPException(400, "Avval yakuniy video tayyor bo'lishi kerak.")
    idea_flow_ok = bool(DARSLIK_API_KEY and IDEA_FLOW_URL)
    telegram_ok = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    if not idea_flow_ok and not telegram_ok:
        raise HTTPException(400, "Botga ulanish sozlanmagan (DARSLIK_API_KEY/IDEA_FLOW_URL yoki "
                                  "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID environment variable'lari kiritilmagan).")

    if idea_flow_ok:
        download_url = f"{str(request.base_url).rstrip('/')}/api/videos/{video_id}/final-download"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{IDEA_FLOW_URL.rstrip('/')}/api/public/darslik/videos",
                headers={"X-Darslik-Api-Key": DARSLIK_API_KEY, "Content-Type": "application/json"},
                json={"title": v["original_name"], "url": download_url},
                timeout=30,
            )
        if resp.status_code >= 400:
            raise HTTPException(502, f"Idea Flow xatosi ({resp.status_code}): {resp.text[:400]}")
        db.execute("UPDATE videos SET idea_flow_sent_at = ? WHERE id = ?", (db.now(), video_id))

    if telegram_ok:
        db.execute("UPDATE videos SET telegram_send_status = 'sending', telegram_send_error = NULL WHERE id = ?",
                   (video_id,))
        asyncio.create_task(_send_video_file_to_telegram(video_id, v["final_video_path"], v["original_name"]))

    return {"ok": True}


@app.post("/api/split-videos/{video_id}/retry")
async def retry_split_send(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ? AND kind = 'split_only'", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["path"] or not Path(v["path"]).exists():
        raise HTTPException(400, "Video fayli topilmadi.")
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise HTTPException(400, "Telegram bot sozlanmagan (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID).")
    db.execute("UPDATE videos SET telegram_send_status = 'sending', telegram_send_error = NULL WHERE id = ?",
               (video_id,))
    asyncio.create_task(_send_video_file_to_telegram(video_id, v["path"], v["original_name"]))
    return {"ok": True}


# ---------------------------------------------------------------------------
#                          RESUMABLE UPLOAD
# ---------------------------------------------------------------------------

@app.post("/api/videos/upload/init")
async def upload_init(original_name: str = Form(...), total_size: int = Form(...), kind: str = Form("pipeline"),
                       file_kind: str = Form("video")):
    if kind not in ("pipeline", "split_only", "cloud"):
        raise HTTPException(400, "Noto'g'ri kind qiymati.")
    if file_kind not in ("video", "image", "file"):
        raise HTTPException(400, "Noto'g'ri file_kind qiymati.")
    if total_size > MAX_UPLOAD_SIZE:
        raise HTTPException(400, f"Fayl juda katta (limit: {MAX_UPLOAD_SIZE // (1024**3)} GB).")
    if not has_space_for(total_size):
        raise HTTPException(400, "Serverda yetarli bo'sh joy yo'q.")

    name = safe_name(original_name)
    if kind != "cloud":
        existing_video = db.fetchone(
            "SELECT * FROM videos WHERE original_name = ? AND file_size = ? AND kind = ? AND status != 'error' LIMIT 1",
            (name, total_size, kind))
        if existing_video and existing_video["status"] != "uploading":
            return {"duplicate_of": video_public(existing_video) if kind == "pipeline" else split_video_public(existing_video)}

    existing_upload = db.fetchone(
        "SELECT * FROM uploads WHERE original_name = ? AND total_size = ? AND kind = ? AND status = 'uploading' LIMIT 1",
        (name, total_size, kind))
    if existing_upload:
        return {"upload_id": existing_upload["id"], "received_size": existing_upload["received_size"], "resumed": True}

    upload_id = db.new_id()
    tmp_path = UPLOADS_DIR / f"{upload_id}.part"
    tmp_path.touch()
    db.execute(
        """INSERT INTO uploads (id, original_name, total_size, received_size, tmp_path, status, kind, file_kind,
           created_at, updated_at) VALUES (?, ?, ?, 0, ?, 'uploading', ?, ?, ?, ?)""",
        (upload_id, name, total_size, str(tmp_path), kind, file_kind, db.now(), db.now()),
    )
    return {"upload_id": upload_id, "received_size": 0, "resumed": False}


@app.get("/api/videos/upload/{upload_id}")
async def upload_status(upload_id: str):
    u = db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    return {"id": u["id"], "received_size": u["received_size"], "total_size": u["total_size"], "status": u["status"]}


@app.post("/api/videos/upload/{upload_id}/chunk")
async def upload_chunk(upload_id: str, offset: int = Form(...), chunk: UploadFile = File(...)):
    u = db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    if u["status"] != "uploading":
        raise HTTPException(400, f"Upload holati '{u['status']}'.")
    if offset != u["received_size"]:
        return JSONResponse({"error": "offset mos kelmadi, qayta sinxronlashtiring.",
                              "received_size": u["received_size"]}, status_code=409)

    tmp_path = Path(u["tmp_path"])
    written = 0
    with tmp_path.open("ab") as out:
        while True:
            part = await chunk.read(1024 * 1024)
            if not part:
                break
            out.write(part)
            written += len(part)

    new_received = u["received_size"] + written
    if not has_space_for(0):
        db.execute("UPDATE uploads SET status = 'error', updated_at = ? WHERE id = ?", (db.now(), upload_id))
        raise HTTPException(400, "Serverda joy tugadi, upload to'xtatildi.")
    db.execute("UPDATE uploads SET received_size = ?, updated_at = ? WHERE id = ?",
               (new_received, db.now(), upload_id))
    return {"received_size": new_received, "total_size": u["total_size"]}


@app.post("/api/videos/upload/{upload_id}/complete")
async def upload_complete(upload_id: str, request: Request):
    u = db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    tmp_path = Path(u["tmp_path"])
    actual_size = tmp_path.stat().st_size if tmp_path.exists() else 0
    if actual_size != u["total_size"]:
        raise HTTPException(400, f"Fayl hajmi mos kelmadi ({actual_size} != {u['total_size']}). "
                                  f"Qolgan qismini yuklashda davom eting.")

    kind = u["kind"] or "pipeline"

    if kind == "cloud":
        cloud_id = db.new_id()
        dest_dir = CLOUD_DIR / cloud_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / u["original_name"]
        shutil.move(str(tmp_path), str(dest_path))
        db.execute(
            """INSERT INTO cloud_files (id, kind, original_name, filename, path, file_size, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cloud_id, u["file_kind"] or "video", u["original_name"], dest_path.name, str(dest_path),
             u["total_size"], db.now()),
        )
        db.execute("UPDATE uploads SET status = 'completed' WHERE id = ?", (upload_id,))
        return {"cloud_file_id": cloud_id}

    video_id = db.new_id()
    dest_dir = VIDEOS_DIR / video_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / u["original_name"]
    shutil.move(str(tmp_path), str(dest_path))

    init_status = "uploaded" if kind == "pipeline" else "completed"
    init_message = "Serverda saqlangan. Bo'laklarga bo'lishni kuting." if kind == "pipeline" else "Yuklandi, botga yuborilmoqda..."
    db.execute(
        """INSERT INTO videos (id, original_name, filename, path, file_size, status, kind,
           created_at, updated_at, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, u["original_name"], dest_path.name, str(dest_path), u["total_size"], init_status, kind,
         db.now(), db.now(), init_message),
    )
    db.execute("UPDATE uploads SET status = 'completed', video_id = ? WHERE id = ?", (video_id, upload_id))
    db.log_line(video_id, "Video serverga to'liq yuklandi.")

    # Davomiylik va thumbnail tezkor hisoblanadi (segmentatsiya emas - u alohida qadam)
    try:
        duration = transcription.get_duration_seconds(dest_path)
        thumb_path = dest_dir / "thumb.jpg"
        has_thumb = transcription.generate_thumbnail(dest_path, thumb_path)
        worker._update_video(video_id, duration=duration,
                              thumbnail_path=str(thumb_path) if has_thumb else None)
    except Exception as e:
        db.log_line(video_id, f"Ogohlantirish: thumbnail/davomiylik olinmadi: {e}")

    if kind == "split_only":
        if DARSLIK_API_KEY and IDEA_FLOW_URL:
            download_url = f"{str(request.base_url).rstrip('/')}/api/videos/{video_id}/original-stream"
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{IDEA_FLOW_URL.rstrip('/')}/api/public/darslik/videos",
                        headers={"X-Darslik-Api-Key": DARSLIK_API_KEY, "Content-Type": "application/json"},
                        json={"title": u["original_name"], "url": download_url},
                        timeout=30,
                    )
                if resp.status_code < 400:
                    db.execute("UPDATE videos SET idea_flow_sent_at = ? WHERE id = ?", (db.now(), video_id))
                else:
                    db.log_line(video_id, f"Idea Flow xatosi ({resp.status_code}): {resp.text[:300]}")
            except Exception as e:
                db.log_line(video_id, f"Idea Flow'ga yozishda xato: {e}")

        if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
            db.execute("UPDATE videos SET telegram_send_status = 'error', telegram_send_error = ? WHERE id = ?",
                       ("Telegram bot sozlanmagan (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID).", video_id))
        else:
            db.execute("UPDATE videos SET telegram_send_status = 'sending' WHERE id = ?", (video_id,))
            asyncio.create_task(_send_video_file_to_telegram(video_id, str(dest_path), u["original_name"]))

    return {"video_id": video_id}


@app.delete("/api/videos/upload/{upload_id}")
async def upload_cancel(upload_id: str):
    u = db.fetchone("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    Path(u["tmp_path"]).unlink(missing_ok=True)
    db.execute("UPDATE uploads SET status = 'cancelled' WHERE id = ?", (upload_id,))
    return {"ok": True}


@app.get("/api/uploads")
async def list_uploads():
    return db.fetchall("SELECT id, original_name, total_size, received_size, status, created_at "
                        "FROM uploads WHERE status IN ('uploading','error') ORDER BY created_at DESC")


@app.delete("/api/uploads/cleanup")
async def cleanup_uploads():
    rows = db.fetchall("SELECT * FROM uploads WHERE status IN ('cancelled', 'error')")
    for u in rows:
        Path(u["tmp_path"]).unlink(missing_ok=True)
        db.execute("DELETE FROM uploads WHERE id = ?", (u["id"],))
    return {"removed": len(rows)}


# ---------------------------------------------------------------------------
#                          BULUT (umumiy fayl saqlash - video/rasm/hujjat)
# ---------------------------------------------------------------------------

def cloud_file_public(f: dict) -> dict:
    return {"id": f["id"], "kind": f["kind"], "original_name": f["original_name"],
            "file_size": f["file_size"], "created_at": f["created_at"]}


@app.post("/api/public/incoming-video")
async def incoming_video_from_bot(request: Request):
    """Tashqi bot (masalan Lovable/Idea Flow'da qurilgan Telegram boti) foydalanuvchidan
    qabul qilgan videoni shu API orqali "Bulut"ga jo'natadi. Bu darslikservetning o'zi
    Idea Flow'ga video jo'natishda ishlatadigan usulning aynan aksi (bir xil
    DARSLIK_API_KEY, bir xil {title, url} shakli) - shuning uchun boshqa tomon ham
    bizga xuddi shunday, video faylning o'zini emas, balki uni yuklab olish mumkin
    bo'lgan URL manzilini jo'natadi (masalan Supabase Storage havolasi), biz esa uni
    o'zimiz oqim (stream) tarzida yuklab olamiz. Bu katta (2 GB gacha) videolar uchun
    ham ishlaydi va Telegram Bot API'ning fayl hajmi cheklovlariga bog'liq emas."""
    if not DARSLIK_API_KEY:
        raise HTTPException(403, "Bu funksiya sozlanmagan (DARSLIK_API_KEY o'rnatilmagan).")
    if request.headers.get("X-Darslik-Api-Key", "") != DARSLIK_API_KEY:
        raise HTTPException(401, "Api-Key noto'g'ri.")

    body = await request.json()
    title = (body.get("title") or "video.mp4").strip()
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "'url' maydoni kerak.")

    if not has_space_for(0):
        raise HTTPException(400, "Serverda joy yetarli emas.")

    name = safe_name(title)
    cloud_id = db.new_id()
    dest_dir = CLOUD_DIR / cloud_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name

    total = 0
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as stream:
                stream.raise_for_status()
                content_length = int(stream.headers.get("content-length") or 0)
                if content_length and content_length > MAX_UPLOAD_SIZE:
                    raise HTTPException(400, "Video hajmi ruxsat etilgan chegaradan katta.")
                with dest_path.open("wb") as f:
                    async for part in stream.aiter_bytes(1024 * 1024):
                        total += len(part)
                        if total > MAX_UPLOAD_SIZE:
                            raise HTTPException(400, "Video hajmi ruxsat etilgan chegaradan katta.")
                        f.write(part)
    except HTTPException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    except httpx.HTTPError as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(400, f"Videoni yuklab olib bo'lmadi: {e}")

    if not has_space_for(0):
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(400, "Serverda joy yetarli emas.")

    db.execute(
        """INSERT INTO cloud_files (id, kind, original_name, filename, path, file_size, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cloud_id, "video", name, dest_path.name, str(dest_path), total, db.now()),
    )
    return {"ok": True, "cloud_file_id": cloud_id}


@app.get("/api/cloud-files")
async def list_cloud_files(kind: str = None):
    if kind:
        rows = db.fetchall("SELECT * FROM cloud_files WHERE kind = ? ORDER BY created_at DESC", (kind,))
    else:
        rows = db.fetchall("SELECT * FROM cloud_files ORDER BY created_at DESC")
    return [cloud_file_public(f) for f in rows]


@app.delete("/api/cloud-files/{cloud_id}")
async def delete_cloud_file(cloud_id: str):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ?", (cloud_id,))
    if not f:
        raise HTTPException(404, "Fayl topilmadi.")
    shutil.rmtree(Path(f["path"]).parent, ignore_errors=True)
    db.execute("DELETE FROM cloud_files WHERE id = ?", (cloud_id,))
    return {"ok": True}


def _move_cloud_video_into_pipeline(f: dict, kind: str) -> str:
    """Bulutdagi video faylni Videolar (pipeline) yoki Video bo'lish (split_only)
    tarkibiga ko'chiradi va cloud_files'dan olib tashlaydi."""
    video_id = db.new_id()
    dest_dir = VIDEOS_DIR / video_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f["original_name"]
    shutil.move(f["path"], str(dest_path))
    init_status = "uploaded" if kind == "pipeline" else "completed"
    init_message = "Bulutdan qo'shildi. Bo'laklarga bo'lishni kuting." if kind == "pipeline" else "Bulutdan qo'shildi, botga yuborilmoqda..."
    db.execute(
        """INSERT INTO videos (id, original_name, filename, path, file_size, status, kind,
           created_at, updated_at, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, f["original_name"], dest_path.name, str(dest_path), f["file_size"], init_status, kind,
         db.now(), db.now(), init_message),
    )
    db.log_line(video_id, "Bulutdan video qo'shildi.")
    try:
        duration = transcription.get_duration_seconds(dest_path)
        thumb_path = dest_dir / "thumb.jpg"
        has_thumb = transcription.generate_thumbnail(dest_path, thumb_path)
        worker._update_video(video_id, duration=duration,
                              thumbnail_path=str(thumb_path) if has_thumb else None)
    except Exception as e:
        db.log_line(video_id, f"Ogohlantirish: thumbnail/davomiylik olinmadi: {e}")
    shutil.rmtree(Path(f["path"]).parent, ignore_errors=True)
    db.execute("DELETE FROM cloud_files WHERE id = ?", (f["id"],))
    return video_id


@app.post("/api/cloud-files/{cloud_id}/use-in-pipeline")
async def use_cloud_file_in_pipeline(cloud_id: str):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND kind = 'video'", (cloud_id,))
    if not f:
        raise HTTPException(404, "Bulutda bunday video topilmadi.")
    if not has_space_for(f["file_size"]):
        raise HTTPException(400, "Serverda yetarli bo'sh joy yo'q.")
    video_id = _move_cloud_video_into_pipeline(f, "pipeline")
    return {"video_id": video_id}


@app.post("/api/cloud-files/{cloud_id}/use-in-split")
async def use_cloud_file_in_split(cloud_id: str, request: Request):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND kind = 'video'", (cloud_id,))
    if not f:
        raise HTTPException(404, "Bulutda bunday video topilmadi.")
    if not has_space_for(f["file_size"]):
        raise HTTPException(400, "Serverda yetarli bo'sh joy yo'q.")
    original_name = f["original_name"]
    video_id = _move_cloud_video_into_pipeline(f, "split_only")

    if DARSLIK_API_KEY and IDEA_FLOW_URL:
        download_url = f"{str(request.base_url).rstrip('/')}/api/videos/{video_id}/original-stream"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{IDEA_FLOW_URL.rstrip('/')}/api/public/darslik/videos",
                    headers={"X-Darslik-Api-Key": DARSLIK_API_KEY, "Content-Type": "application/json"},
                    json={"title": original_name, "url": download_url}, timeout=30,
                )
            if resp.status_code < 400:
                db.execute("UPDATE videos SET idea_flow_sent_at = ? WHERE id = ?", (db.now(), video_id))
        except Exception as e:
            db.log_line(video_id, f"Idea Flow'ga yozishda xato: {e}")

    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        db.execute("UPDATE videos SET telegram_send_status = 'sending' WHERE id = ?", (video_id,))
        asyncio.create_task(_send_video_file_to_telegram(video_id, v["path"], v["original_name"]))
    else:
        db.execute("UPDATE videos SET telegram_send_status = 'error', telegram_send_error = ? WHERE id = ?",
                   ("Telegram bot sozlanmagan (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID).", video_id))
    return {"video_id": video_id}


# ---------------------------------------------------------------------------
#                          JOB (TRANSKRIPSIYA) BOSHQARUVI
# ---------------------------------------------------------------------------

PHASE_LABELS = {
    "uploaded": "Yuklandi", "segmenting": "Bo'laklanmoqda", "segments_ready": "Bo'laklar tayyor",
    "transcribing": "Transkripsiya", "transcription_ready": "Matn tayyor",
    "transcription_approved": "Matn tasdiqlangan", "translation_ready": "Tarjima tayyor",
    "audio_processing": "Audio yaratilmoqda", "audio_ready": "Audio tayyor",
    "video_rendering": "Video yig'ilmoqda", "completed": "Tugallangan",
    "failed": "Xato", "cancelled": "Bekor qilingan",
}


@app.get("/api/jobs")
async def list_jobs():
    videos = db.fetchall(
        "SELECT id, original_name, status, blocked_reason, progress, message, chunk_count, created_at FROM videos "
        "WHERE status NOT IN ('uploading') ORDER BY created_at DESC")
    video_jobs = [{"id": v["id"], "type": "video", "title": v["original_name"], "status": v["status"],
                    "blocked_reason": v["blocked_reason"], "phase": PHASE_LABELS.get(v["status"], v["status"]),
                    "progress": v["progress"], "message": v["message"], "total": v["chunk_count"]} for v in videos]
    tts_jobs = db.fetchall(
        "SELECT id, title, status, total_segments, completed_segments, error FROM tts_jobs "
        "WHERE video_id IS NULL ORDER BY created_at DESC")
    tts_job_list = [{"id": t["id"], "type": "tts", "title": t["title"], "status": t["status"],
                      "progress": round((t["completed_segments"] / t["total_segments"] * 100), 1) if t["total_segments"] else 0,
                      "message": t["error"] or "", "total": t["total_segments"],
                      "completed": t["completed_segments"]} for t in tts_jobs]
    return {"video_jobs": video_jobs, "tts_jobs": tts_job_list}


def _ensure_video(video_id: str) -> dict:
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    return v


@app.post("/api/jobs/{video_id}/pause")
async def job_pause(video_id: str):
    _ensure_video(video_id)
    worker.pause_job(video_id)
    return {"ok": True}


@app.post("/api/jobs/{video_id}/resume")
async def job_resume(video_id: str):
    _ensure_video(video_id)
    worker.resume_job(video_id)
    return {"ok": True}


@app.post("/api/jobs/{video_id}/retry")
async def job_retry(video_id: str):
    _ensure_video(video_id)
    worker.resume_job(video_id)
    return {"ok": True}


@app.post("/api/jobs/{video_id}/cancel")
async def job_cancel(video_id: str):
    _ensure_video(video_id)
    worker.cancel_job(video_id)
    return {"ok": True}


@app.post("/api/jobs/{video_id}/retry-chunk/{chunk_id}")
async def job_retry_chunk(video_id: str, chunk_id: str):
    _ensure_video(video_id)
    worker.retry_chunk(video_id, chunk_id)
    return {"ok": True}


@app.get("/api/videos/{video_id}/chunks/{chunk_id}/audio")
async def download_chunk_audio(video_id: str, chunk_id: str):
    """Bitta 5 daqiqalik bo'lakning audiosini (mp3) yuklab olish uchun - foydalanuvchi
    uni tashqarida tinglab/qayta ishlab, tayyor matnini keyin yuklab qaytarishi uchun."""
    c = db.fetchone("SELECT * FROM chunks WHERE id = ? AND video_id = ?", (chunk_id, video_id))
    if not c:
        raise HTTPException(404, "Bo'lak topilmadi.")
    if not c["path"] or not Path(c["path"]).exists():
        raise HTTPException(404, "Bo'lak audio fayli topilmadi.")
    return FileResponse(c["path"], media_type="audio/mpeg", filename=f"bolak_{c['chunk_index'] + 1}.mp3")


@app.get("/api/videos/{video_id}/chunks/zip")
async def download_all_chunks_zip(video_id: str):
    """Butun videoning barcha 5 daqiqalik bo'laklari audiosini bitta ZIP faylga
    yig'ib beradi - har birini alohida-alohida yuklab olishning o'rniga."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    chunks = db.fetchall("SELECT * FROM chunks WHERE video_id = ? ORDER BY chunk_index ASC", (video_id,))
    available = [c for c in chunks if c["path"] and Path(c["path"]).exists()]
    if not available:
        raise HTTPException(404, "Hech qanday bo'lak audiosi topilmadi.")

    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for c in available:
            zf.write(c["path"], arcname=f"bolak_{c['chunk_index'] + 1:02d}.mp3")
    buf.seek(0)

    base = safe_name(Path(v["original_name"]).stem) or "video"
    return StreamingResponse(buf, media_type="application/zip",
                              headers={"Content-Disposition": f'attachment; filename="{base}_bolaklar.zip"'})


@app.post("/api/videos/{video_id}/chunks/{chunk_id}/replace-transcript")
async def replace_chunk_transcript_endpoint(video_id: str, chunk_id: str, file: UploadFile = File(...)):
    """Bitta bo'lakning Whisper natijasini, foydalanuvchi tashqarida tayyorlagan
    matn (butun bo'lak uchun gaplarga bo'lib taxminiy vaqt beriladi) yoki SRT (bo'lak
    ichidagi aniq vaqt bilan, 0:00 = shu bo'lak boshlanishi) fayli bilan to'liq
    almashtiradi. Format fayl kengaytmasiga emas, balki ichidagi mazmuniga qarab
    aniqlanadi (mobil brauzerlarda kengaytma noto'g'ri kelishi mumkin)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    c = db.fetchone("SELECT * FROM chunks WHERE id = ? AND video_id = ?", (chunk_id, video_id))
    if not c:
        raise HTTPException(404, "Bo'lak topilmadi.")

    raw = await file.read()
    name = (file.filename or "").lower()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="ignore")

    looks_like_srt = name.endswith(".srt") or bool(
        re.search(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}", text))

    if looks_like_srt:
        try:
            local_segments = translation.parse_srt_direct(text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        segments = [{"start": s["start"] + c["start_time"], "end": s["end"] + c["start_time"], "text": s["text"]}
                    for s in local_segments]
    else:
        clean_text = text.strip()
        if not clean_text:
            raise HTTPException(400, "Fayl bo'sh.")
        segments = transcription.split_plain_text_into_segments(clean_text, c["start_time"], c["end_time"])
        if not segments:
            raise HTTPException(400, "Fayldan matn topilmadi.")

    try:
        worker.replace_chunk_transcript(video_id, chunk_id, segments)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await worker.finalize_results(video_id)
    return {"ok": True, "segment_count": len(segments)}


@app.post("/api/jobs/{video_id}/cancel-chunk/{chunk_id}")
async def job_cancel_chunk(video_id: str, chunk_id: str):
    """Hozir Whisper API'ga so'rov yuborib, sekinlashib/osilib qolgan bo'lakni
    majburan bekor qilib, qayta navbatga qo'yadi (kutishga hojat qolmaydi)."""
    _ensure_video(video_id)
    task = worker.RUNNING_CHUNK_TASKS.get(chunk_id)
    if not task:
        raise HTTPException(400, "Bu bo'lak hozir ishlamayapti (allaqachon tugagan yoki navbatda).")
    task.cancel()
    return {"ok": True}


@app.post("/api/jobs/{video_id}/retry-range")
async def job_retry_range(video_id: str, start_time: float = Form(...), end_time: float = Form(...)):
    _ensure_video(video_id)
    if end_time <= start_time:
        raise HTTPException(400, "Tugash vaqti boshlanish vaqtidan katta bo'lishi kerak.")
    n = worker.retry_range(video_id, start_time, end_time)
    return {"ok": True, "chunks_queued": n}


# ---------------------------------------------------------------------------
#                          NATIJALAR
# ---------------------------------------------------------------------------

@app.get("/api/videos/{video_id}/results")
async def video_results(video_id: str):
    _ensure_video(video_id)
    return db.fetchall("SELECT id, kind, filename, created_at FROM results WHERE video_id = ?", (video_id,))


@app.get("/api/videos/{video_id}/final-download")
async def download_final_video(video_id: str, request: Request):
    v = _ensure_video(video_id)
    if not v["final_video_path"] or not Path(v["final_video_path"]).exists():
        raise HTTPException(404, "Yakuniy video topilmadi.")
    return range_file_response(request, Path(v["final_video_path"]), "video/mp4")


@app.get("/api/videos/{video_id}/original-stream")
async def stream_original_video(video_id: str, request: Request):
    v = _ensure_video(video_id)
    if not v["path"] or not Path(v["path"]).exists():
        raise HTTPException(404, "Original video topilmadi.")
    return range_file_response(request, Path(v["path"]), "video/mp4")


def _result_by_kind(video_id: str, kind: str):
    return db.fetchone("SELECT * FROM results WHERE video_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 1",
                        (video_id, kind))


@app.get("/api/videos/{video_id}/subtitles/original.vtt")
async def subtitles_original_vtt(video_id: str):
    r = _result_by_kind(video_id, "vtt_original")
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "Original subtitr topilmadi.")
    return FileResponse(r["path"], media_type="text/vtt")


@app.get("/api/videos/{video_id}/subtitles/uz.vtt")
async def subtitles_uz_vtt(video_id: str):
    r = _result_by_kind(video_id, "vtt_uz")
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "O'zbekcha subtitr topilmadi.")
    return FileResponse(r["path"], media_type="text/vtt")


@app.get("/api/results/{result_id}/download")
async def download_result(result_id: str):
    r = db.fetchone("SELECT * FROM results WHERE id = ?", (result_id,))
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "Natija topilmadi.")
    return FileResponse(r["path"], filename=r["filename"], media_type="text/plain; charset=utf-8")


@app.get("/api/results/{result_id}/view")
async def view_result(result_id: str):
    r = db.fetchone("SELECT * FROM results WHERE id = ?", (result_id,))
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "Natija topilmadi.")
    return {"filename": r["filename"], "content": Path(r["path"]).read_text(encoding="utf-8")}


# ---------------------------------------------------------------------------
#                          API KALITLAR
# ---------------------------------------------------------------------------

@app.get("/api/api-keys")
async def list_api_keys(provider: str = None):
    return keys_manager.list_keys_public(provider)


@app.post("/api/api-keys")
async def add_api_key(key: str = Form(...), label: str = Form(""), provider: str = Form("openai")):
    try:
        return keys_manager.add_key(key, label, provider)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/api-keys/{key_id}")
async def delete_api_key(key_id: str):
    keys_manager.delete_key(key_id)
    return {"ok": True}


@app.post("/api/api-keys/{key_id}/toggle")
async def toggle_api_key(key_id: str, active: bool = Form(...)):
    keys_manager.set_active(key_id, active)
    return {"ok": True}


@app.post("/api/api-keys/{key_id}/test")
async def test_api_key(key_id: str):
    return await keys_manager.test_key(key_id)


# ---------------------------------------------------------------------------
#                          MATN -> AUDIO (TTS) - BACKEND JOB
# ---------------------------------------------------------------------------

def parse_srt(content: str) -> list:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n\s*\n", normalized)
    time_re = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
    result = []
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip() != ""]
        if not lines:
            continue
        idx = 1 if re.match(r"^\d+$", lines[0].strip()) else 0
        if idx >= len(lines):
            continue
        m = time_re.search(lines[idx])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = " ".join(lines[idx + 1:]).strip()
        if text:
            result.append({"start": start, "end": end, "text": text})
    return result


@app.post("/api/tts/jobs")
async def create_tts_job(
    title: str = Form(""),
    provider: str = Form(...),
    srt: str = Form(...),
    voice: str = Form(""),
    mood: str = Form(""),
    speed: float = Form(1.0),
    instructions: str = Form(""),
    aisha_key: str = Form(""),
    stretch_to_fit: bool = Form(True),
):
    segments = parse_srt(srt)
    if not segments:
        raise HTTPException(400, "SRT formatidagi bloklar topilmadi.")
    if provider == "aisha" and not aisha_key.strip():
        raise HTTPException(400, "Aisha API kalit kiritilmagan.")
    if provider == "openai" and not keys_manager.has_any_active_key():
        raise HTTPException(400, "Ishlaydigan OpenAI API kalit topilmadi. Avval API kalit qo'shing.")
    job_id = tts.create_job(title, provider, segments, voice, mood, speed, instructions,
                             aisha_key.strip(), stretch_to_fit)
    return {"id": job_id, "segments": len(segments)}


@app.get("/api/tts/jobs")
async def list_tts_jobs():
    return db.fetchall("SELECT id, title, provider, status, total_segments, completed_segments, "
                        "error, created_at FROM tts_jobs ORDER BY created_at DESC")


@app.get("/api/tts/jobs/{job_id}")
async def get_tts_job(job_id: str):
    j = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    if not j:
        raise HTTPException(404, "Ish topilmadi.")
    segs = db.fetchall("SELECT seg_index, start_sec, end_sec, text, status, error FROM tts_segments "
                        "WHERE job_id = ? ORDER BY seg_index ASC", (job_id,))
    logs = db.get_logs(job_id, 200)
    j = dict(j)
    j.pop("aisha_key_encrypted", None)
    j["segments"] = segs
    j["logs"] = logs
    return j


@app.post("/api/tts/jobs/{job_id}/pause")
async def tts_pause(job_id: str):
    tts.pause_job(job_id)
    return {"ok": True}


@app.post("/api/tts/jobs/{job_id}/resume")
async def tts_resume(job_id: str):
    tts.resume_job(job_id)
    return {"ok": True}


@app.post("/api/tts/jobs/{job_id}/retry")
async def tts_retry(job_id: str):
    tts.retry_job(job_id)
    return {"ok": True}


@app.post("/api/tts/jobs/{job_id}/cancel")
async def tts_cancel(job_id: str):
    tts.cancel_job(job_id)
    return {"ok": True}


@app.delete("/api/tts/jobs/{job_id}")
async def tts_delete(job_id: str):
    j = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    if not j:
        raise HTTPException(404, "Ish topilmadi.")
    db.execute("DELETE FROM tts_segments WHERE job_id = ?", (job_id,))
    db.execute("DELETE FROM tts_jobs WHERE id = ?", (job_id,))
    from storage import TTS_DIR
    shutil.rmtree(TTS_DIR / job_id, ignore_errors=True)
    return {"ok": True}


@app.get("/api/tts/jobs/{job_id}/download")
async def tts_download(job_id: str, request: Request):
    j = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    if not j or not j["result_path"] or not Path(j["result_path"]).exists():
        raise HTTPException(404, "Yakuniy audio topilmadi.")
    return range_file_response(request, Path(j["result_path"]), "audio/mpeg")


# ---------------------------------------------------------------------------
#                          XARAJATLAR
# ---------------------------------------------------------------------------

@app.get("/api/costs")
async def get_costs():
    import time
    now = time.time()
    today_start = time.strftime("%Y-%m-%dT00:00:00", time.gmtime(now))
    week_start = time.strftime("%Y-%m-%dT00:00:00", time.gmtime(now - 6 * 86400))
    month_start = time.strftime("%Y-%m-%dT00:00:00", time.gmtime(now - 29 * 86400))

    def total_since(since):
        r = db.fetchone("SELECT COALESCE(SUM(amount_usd),0) s FROM costs WHERE created_at >= ?", (since,))
        return round(r["s"], 4)

    per_video = db.fetchall(
        """SELECT v.id, v.original_name, COALESCE(SUM(c.amount_usd),0) as total,
           SUM(CASE WHEN c.kind='transcription' THEN c.amount_usd ELSE 0 END) as transcription,
           SUM(CASE WHEN c.kind='translation' THEN c.amount_usd ELSE 0 END) as translation,
           SUM(CASE WHEN c.kind='tts' THEN c.amount_usd ELSE 0 END) as tts
           FROM videos v LEFT JOIN costs c ON c.video_id = v.id
           GROUP BY v.id ORDER BY v.created_at DESC""")
    return {
        "today": total_since(today_start),
        "week": total_since(week_start),
        "month": total_since(month_start),
        "all_time": total_since("0000-01-01T00:00:00"),
        "per_video": [dict(r) for r in per_video],
    }


@app.get("/api/videos/{video_id}/costs")
async def get_video_costs(video_id: str):
    return db.fetchall("SELECT kind, amount_usd, detail, created_at FROM costs "
                        "WHERE video_id = ? ORDER BY created_at ASC", (video_id,))


# ---------------------------------------------------------------------------
#                          SOZLAMALAR (Aysha va OpenAI standart qiymatlari)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "aisha_default_voice": "Gulnoza", "aisha_default_mood": "Neutral", "aisha_default_speed": "1.0",
    "openai_default_voice": "alloy", "openai_default_instructions": "",
    "default_language": "", "default_stretch_to_fit": "true",
    "translation_instruction": "", "translation_context": "",
}


@app.get("/api/settings")
async def get_settings():
    out = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        v = db.get_setting(key)
        if v is not None:
            out[key] = v
    return out


@app.post("/api/settings")
async def update_settings(payload: dict):
    for key, value in payload.items():
        if key in DEFAULT_SETTINGS:
            db.set_setting(key, str(value))
    return {"ok": True}


# ---------------------------------------------------------------------------
#            TARJIMON XOTIRASI (Claude bilan kichik chat + qoidalar)
# ---------------------------------------------------------------------------

@app.get("/api/translation-memory/chat")
async def get_memory_chat():
    return db.fetchall("SELECT id, role, content, created_at FROM translation_memory_chat "
                        "ORDER BY created_at ASC")


@app.post("/api/translation-memory/chat")
async def send_memory_chat(message: str = Form(...)):
    if not keys_manager.has_any_active_key(provider="claude"):
        raise HTTPException(400, "Ishlaydigan Claude API kalit topilmadi. Avval API kalit qo'shing.")
    kid, raw = keys_manager.get_next_active_key(provider="claude")

    user_msg_id = db.new_id()
    db.execute("INSERT INTO translation_memory_chat (id, role, content, created_at) VALUES (?, 'user', ?, ?)",
               (user_msg_id, message, db.now()))

    history = db.fetchall("SELECT role, content FROM translation_memory_chat ORDER BY created_at ASC LIMIT 40")
    claude_messages = [{"role": h["role"], "content": h["content"]} for h in history]

    system_prompt = (
        "Siz Darslik Studiyasi dasturidagi tarjima sifatini yaxshilashga yordam beruvchi "
        "yordamchisiz. Foydalanuvchi sizga tarjimadagi xato yoki tuzatish kerak bo'lgan "
        "qoidalarni aytadi. Har bir javobingizda, agar foydalanuvchi aniq bir qoida "
        "(masalan 'X so'zini Y deb tarjima qil') aytgan bo'lsa, buni tan olib qisqa "
        "tasdiqlang. O'zbek va rus/ingliz tillarida, stomatologiya sohasida yordam berasiz."
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": raw, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1024,
                      "system": system_prompt, "messages": claude_messages},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Claude xatosi ({resp.status_code}): {resp.text[:400]}")
        data = resp.json()
        reply = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        keys_manager.mark_result(kid, True)
    except Exception as e:
        keys_manager.mark_result(kid, False, str(e))
        raise HTTPException(400, f"Claude bilan bog'lanishda xato: {e}")

    assistant_msg_id = db.new_id()
    db.execute("INSERT INTO translation_memory_chat (id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
               (assistant_msg_id, reply, db.now()))
    return {"id": assistant_msg_id, "role": "assistant", "content": reply}


@app.delete("/api/translation-memory/chat")
async def clear_memory_chat():
    db.execute("DELETE FROM translation_memory_chat", ())
    return {"ok": True}


@app.get("/api/translation-memory/notes")
async def list_memory_notes():
    return db.fetchall("SELECT id, content, created_at FROM translation_memory_notes ORDER BY created_at DESC")


@app.post("/api/translation-memory/notes")
async def add_memory_note(content: str = Form(...), source_message_id: str = Form("")):
    note_id = db.new_id()
    db.execute("INSERT INTO translation_memory_notes (id, content, source_message_id, created_at) "
               "VALUES (?, ?, ?, ?)", (note_id, content, source_message_id or None, db.now()))
    return {"ok": True, "id": note_id}


@app.delete("/api/translation-memory/notes/{note_id}")
async def delete_memory_note(note_id: str):
    db.execute("DELETE FROM translation_memory_notes WHERE id = ?", (note_id,))
    return {"ok": True}
