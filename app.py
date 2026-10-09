"""
Darslik Studiyasi - Bulutli server (persistent job tizimi)

Ishga tushirish (lokal sinov uchun):
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000

Oracle Cloud'ga joylashtirish uchun README.txt'ga qarang.
"""
import asyncio
import os
import httpx
import json
import re
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

import database as db
import auth
import keys_manager
import stt_words
import transcription
import translation
import glossary_data
import learning
import tts
import worker
import ideaflow_bot
import bot_section
import cloud_zip
from storage import (VIDEOS_DIR, RESULTS_DIR, UPLOADS_DIR, CHUNKS_DIR, SPLIT_DIR, CLOUD_DIR, MAX_UPLOAD_SIZE, ADMIN_TOKEN,
                      UPLOAD_CHUNK_SIZE, CHUNK_SECONDS, MAX_WHISPER_CONCURRENCY, MAX_ACTIVE_VIDEO_JOBS,
                      MAX_ACTIVE_TTS_JOBS, REPETITION_THRESHOLD, DARSLIK_API_KEY,
                      TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, LOCAL_BOT_API_URL, INBOUND_BOT_TOKEN,
                      APP_USERNAME, APP_PASSWORD, safe_name, disk_usage, has_space_for,
                      TRANSCRIBE_LANGUAGE_CODES, TRANSCRIBE_LANGUAGES)


def _validate_transcribe_language(language: str):
    """Whisper so'roviga yuboriladigan til kodini tekshiradi - noto'g'ri kod
    OpenAI'ga borib qimmatga tushmasdan, shu yerda o'zbekcha tushunarli xato
    bilan qaytariladi."""
    if language not in TRANSCRIBE_LANGUAGE_CODES:
        raise HTTPException(400, f"Noto'g'ri til kodi: '{language}'. Ro'yxatdan tanlang.")

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
app.include_router(bot_section.router)
app.include_router(cloud_zip.router)

# Diqqat: production uchun bu yerga faqat o'zingizning sayt manzilingizni yozing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Native Basic Auth o'rniga xavfsiz HttpOnly cookie-session ishlatadi."""
    path = request.url.path
    public = path in ("/login", "/api/auth/login", "/health", "/manifest.json", "/sw.js") \
        or path.startswith("/icons/") or path.startswith("/api/public/")
    if public:
        return await call_next(request)

    user = auth.user_from_request(request)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Avval tizimga kiring."}, status_code=401)
        return Response(status_code=303, headers={"Location": "/login"})

    request.state.user = user
    token = auth.set_current_user(user)
    try:
        auth.authorize_resource(request, user)
        return await call_next(request)
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    finally:
        auth.reset_current_user(token)


@app.on_event("startup")
async def on_startup():
    db.init_db()
    auth.bootstrap_superadmin(APP_USERNAME, APP_PASSWORD)
    await worker.recover_and_start()
    # Server qayta ishga tushganda yarimda qolgan "Video bo'lish" ishlari.
    for row in db.fetchall("SELECT id FROM videos WHERE kind = 'split_only' AND split_status = 'splitting'"):
        asyncio.create_task(_prepare_split_video(row["id"]))
    for row in db.fetchall("SELECT id FROM videos WHERE kind = 'split_only' AND split_status = 'restoring'"):
        asyncio.create_task(_restore_split_video(row["id"]))
    db.execute("UPDATE videos SET telegram_send_status = 'error', telegram_send_error = ? "
               "WHERE kind = 'split_only' AND telegram_send_status = 'sending'",
               ("Server qayta ishga tushdi - yuborish to'xtab qoldi. \"Davom ettirish\"ni bosing.",))
    if INBOUND_BOT_TOKEN:
        import telegram_bot
        asyncio.create_task(telegram_bot.poll_updates())
    bot_section.recover_interrupted_uploads()
    cloud_zip.recover_interrupted()
    ideaflow_bot.start()


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


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse((BASE / "login.html").read_text(encoding="utf-8"))


@app.post("/api/auth/login")
async def login(request: Request):
    payload = await request.json()
    user = auth.authenticate(str(payload.get("username", "")), str(payload.get("password", "")))
    if not user:
        raise HTTPException(401, "Login yoki parol noto'g'ri.")
    token, _ = auth.create_session(user["id"])
    response = JSONResponse({"ok": True, "user": auth.public_user(user)})
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, secure=forwarded_proto == "https", samesite="lax", path="/")
    return response


@app.post("/api/auth/logout")
async def logout(request: Request):
    auth.delete_session(request)
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
async def auth_me():
    user = auth.current_user()
    out = auth.public_user(user)
    out["usage_bytes"] = auth.user_usage(user["id"])
    return out


@app.get("/api/admin/users")
async def admin_users():
    auth.require_superadmin()
    rows = db.fetchall("SELECT * FROM users ORDER BY CASE role WHEN 'superadmin' THEN 0 ELSE 1 END, created_at")
    return [{**auth.public_user(row), "usage_bytes": auth.user_usage(row["id"])} for row in rows]


@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    auth.require_superadmin()
    payload = await request.json()
    display_name = str(payload.get("display_name", "")).strip()
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not (2 <= len(display_name) <= 120):
        raise HTTPException(400, "Ism-familiya 2-120 belgidan iborat bo'lishi kerak.")
    if not re.fullmatch(r"[A-Za-z0-9@._+-]{3,120}", username):
        raise HTTPException(400, "Login 3-120 belgi bo'lsin; harf, raqam, @ . _ + - ishlatish mumkin.")
    if len(password) < 8:
        raise HTTPException(400, "Parol kamida 8 belgidan iborat bo'lishi kerak.")
    if auth.active_regular_user_count() >= auth.MAX_REGULAR_USERS:
        raise HTTPException(
            400,
            f"Ko'pi bilan {auth.MAX_REGULAR_USERS} ta faol oddiy foydalanuvchi qo'shish mumkin.",
        )
    # Oddiy hisoblarning kvotasi server siyosati bo'yicha doim bir xil (10 GB).
    # Frontend payload'i bu limitni oshira olmaydi.
    quota_bytes = auth.USER_QUOTA
    if auth.allocated_capacity() + quota_bytes > auth.TOTAL_CAPACITY:
        raise HTTPException(400, "180 GB umumiy joydan ajratilmagan qismi yetarli emas.")
    if db.fetchone("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)):
        raise HTTPException(409, "Bu login band.")
    user_id = db.new_id()
    db.execute(
        "INSERT INTO users (id, username, display_name, password_hash, role, quota_bytes, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'user', ?, 1, ?, ?)",
        (user_id, username, display_name, auth.hash_password(password), quota_bytes, db.now(), db.now()),
    )
    return auth.public_user(db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,)))


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: str, request: Request):
    admin = auth.require_superadmin()
    user = db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "Foydalanuvchi topilmadi.")
    payload = await request.json()
    display_name = None
    username = None
    password = None
    credentials_changed = False
    if "display_name" in payload:
        display_name = str(payload["display_name"]).strip()
        if not (2 <= len(display_name) <= 120):
            raise HTTPException(400, "Ism-familiya 2-120 belgidan iborat bo'lishi kerak.")
    if "username" in payload:
        username = str(payload["username"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9@._+-]{3,120}", username):
            raise HTTPException(400, "Login 3-120 belgi bo'lsin; harf, raqam, @ . _ + - ishlatish mumkin.")
        duplicate = db.fetchone(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE AND id <> ?",
            (username, user_id),
        )
        if duplicate:
            raise HTTPException(409, "Bu login band.")
    if "password" in payload and str(payload["password"]):
        password = str(payload["password"])
        if len(password) < 8:
            raise HTTPException(400, "Parol kamida 8 belgidan iborat bo'lishi kerak.")
    if display_name is not None:
        db.execute("UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
                   (display_name, db.now(), user_id))
    if username is not None and username != str(user["username"]):
        db.execute("UPDATE users SET username = ?, updated_at = ? WHERE id = ?",
                   (username, db.now(), user_id))
        credentials_changed = True
    if password is not None:
        db.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                   (auth.hash_password(password), db.now(), user_id))
        credentials_changed = True
    if credentials_changed:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    if "active" in payload:
        if user_id == admin["id"] and not bool(payload["active"]):
            raise HTTPException(400, "O'zingizning super-admin hisobingizni o'chira olmaysiz.")
        if bool(payload["active"]) and not user["active"] and \
                auth.allocated_capacity(user_id) + int(user["quota_bytes"]) > auth.TOTAL_CAPACITY:
            raise HTTPException(400, "Hisobni yoqish uchun 180 GB umumiy hovuzda yetarli joy ajratilmagan.")
        if bool(payload["active"]) and not user["active"] and user["role"] == "user" and \
                auth.active_regular_user_count(user_id) >= auth.MAX_REGULAR_USERS:
            raise HTTPException(
                400,
                f"Ko'pi bilan {auth.MAX_REGULAR_USERS} ta faol oddiy foydalanuvchi bo'lishi mumkin.",
            )
        db.execute("UPDATE users SET active = ?, updated_at = ? WHERE id = ?",
                   (1 if payload["active"] else 0, db.now(), user_id))
        if not payload["active"]:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    if "quota_gb" in payload:
        quota_bytes = int(float(payload["quota_gb"]) * 1024**3)
        if user["role"] == "user" and quota_bytes > auth.USER_QUOTA:
            raise HTTPException(400, "Oddiy foydalanuvchi kvotasi 10 GB dan oshmaydi.")
        if quota_bytes < auth.user_usage(user_id):
            raise HTTPException(400, "Kvota foydalanuvchining hozirgi fayllaridan kichik bo'la olmaydi.")
        if auth.allocated_capacity(user_id) + quota_bytes > auth.TOTAL_CAPACITY:
            raise HTTPException(400, "180 GB umumiy joydan ajratilmagan qismi yetarli emas.")
        db.execute("UPDATE users SET quota_bytes = ?, updated_at = ? WHERE id = ?",
                   (quota_bytes, db.now(), user_id))
    return auth.public_user(db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,)))


@app.get("/api/storage")
async def api_storage():
    user = auth.current_user()
    physical = disk_usage()
    return {
        "app_usage": auth.user_usage(user["id"]),
        "storage_limit": int(user["quota_bytes"]),
        "disk_free": physical["disk_free"],
        "disk_total": physical["disk_total"],
        "is_superadmin": user["role"] == "superadmin",
        "total_capacity": auth.TOTAL_CAPACITY,
        "allocated_capacity": auth.allocated_capacity() if user["role"] == "superadmin" else None,
        "max_regular_users": auth.MAX_REGULAR_USERS if user["role"] == "superadmin" else None,
        "active_regular_users": auth.active_regular_user_count() if user["role"] == "superadmin" else None,
    }


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

def _json_or_none(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def video_public(v: dict) -> dict:
    primary_tts_provider = None
    tempo_stats = None
    if v["tts_job_id"]:
        pj = db.fetchone("SELECT provider, tempo_stats FROM tts_jobs WHERE id = ?", (v["tts_job_id"],))
        primary_tts_provider = pj["provider"] if pj else None
        tempo_stats = _json_or_none(pj["tempo_stats"]) if pj else None
    learning_track = db.fetchone(
        "SELECT srt_filename, srt_status, segment_count, provider, tts_job_id, audio_status, "
        "final_video_status, error, updated_at, export_status, export_with_intro, export_error, "
        "intro_status, intro_progress, intro_message, intro_error, intro_duration, "
        "subtitled_video_status, subtitled_video_error "
        "FROM learning_tracks WHERE video_id = ?", (v["id"],))
    learning_public = dict(learning_track) if learning_track else None
    if learning_public and learning_track["tts_job_id"]:
        learning_job = db.fetchone(
            "SELECT status, total_segments, completed_segments FROM tts_jobs WHERE id = ?",
            (learning_track["tts_job_id"],))
        if learning_job:
            learning_public["job_status"] = learning_job["status"]
            learning_public["total_segments"] = learning_job["total_segments"] or 0
            learning_public["completed_segments"] = learning_job["completed_segments"] or 0
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
        "subtitled_video_status": v["subtitled_video_status"] or "none",
        "subtitled_video_error": v["subtitled_video_error"],
        "bot_upload_status": v["bot_upload_status"] or "none", "bot_upload_error": v["bot_upload_error"],
        "bot_upload_progress": v["bot_upload_progress"], "bot_item_id": v["bot_item_id"],
        "bot_variants": (lambda found: (["project"] + found) if found else found)(
            [name for name, (column, _) in bot_section.VARIANTS.items() if v[column] and Path(v[column]).exists()]),
        "cost_total": v["cost_total"] or 0,
        "cost_total_som": v["cost_total_som"] or 0,
        "has_thumbnail": bool(v["thumbnail_path"]),
        "flagged_issues_count": len(json.loads(v["flagged_issues"])) if v["flagged_issues"] else 0,
        "created_at": v["created_at"], "updated_at": v["updated_at"],
        "primary_tts_provider": primary_tts_provider,
        # Video 'completed' bo'lgach ikkinchi provayder bilan yaratilgan
        # qo'shimcha audio/video track(lar) - odatda bo'sh ro'yxat.
        "audio_tracks": [_track_public(r) for r in db.fetchall(
            "SELECT provider, audio_status, final_video_status, subtitled_video_status, "
            "subtitled_video_error, error, freeze_points FROM audio_tracks WHERE video_id = ?",
            (v["id"],))] if v["status"] == "completed" else [],
        "timeline_summary": transcription.timeline_summary(_parse_points(v["freeze_points"])),
        # Ovoz tezligi statistikasi (gaplar tempo min/max/o'rtacha) - natijada ko'rsatiladi.
        "tempo_stats": tempo_stats,
        # "Ruscha o'rganish" treki - Uzbek pipeline holatidan mustaqil, doim
        # ko'rsatiladi (video hali 'completed' bo'lmasa ham Learning SRT
        # yuklab, audio/video yaratish mumkin).
        "learning_track": learning_public,
    }


def _track_public(r: dict) -> dict:
    d = dict(r)
    d["timeline_summary"] = transcription.timeline_summary(_parse_points(d.pop("freeze_points", None)))
    return d


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
        # None = bo'lak uchun alohida til belgilanmagan (videoning umumiy tilidan
        # foydalaniladi) - frontend "Qayta yuborish" oynasida shuni bilib, standart
        # tanlovni to'g'ri ko'rsatishi uchun kerak.
        "language": c["language"] if "language" in c.keys() else None,
        "running_seconds": round(time.time() - running_since, 0) if running_since else None,
        "segments": segments,
    }


@app.get("/api/videos")
async def list_videos():
    owner_id = auth.current_user_id()
    rows = db.fetchall("SELECT * FROM videos WHERE owner_id = ? AND (kind = 'pipeline' OR kind IS NULL) "
                       "ORDER BY created_at DESC", (owner_id,))
    return [video_public(r) for r in rows]


def split_video_public(v: dict) -> dict:
    return {
        "id": v["id"], "original_name": v["original_name"], "file_size": v["file_size"],
        "duration": v["duration"], "status": v["status"],
        "telegram_send_status": v["telegram_send_status"] or "none", "telegram_send_error": v["telegram_send_error"],
        "split_total_parts": v["split_total_parts"] or 0, "split_parts_sent": v["split_parts_sent"] or 0,
        "split_status": v["split_status"] or "none", "split_error": v["split_error"],
        "split_restore_target": v["split_restore_target"] or "",
        "original_exists": bool(v["path"] and Path(v["path"]).exists()),
        "bot_upload_status": v["bot_upload_status"] or "none", "bot_upload_error": v["bot_upload_error"],
        "bot_upload_progress": v["bot_upload_progress"] or "",
        "has_thumbnail": bool(v["thumbnail_path"]),
        "created_at": v["created_at"], "updated_at": v["updated_at"],
    }


@app.get("/api/split-videos")
async def list_split_videos():
    rows = db.fetchall("SELECT * FROM videos WHERE owner_id = ? AND kind = 'split_only' ORDER BY created_at DESC",
                       (auth.current_user_id(),))
    return [split_video_public(r) for r in rows]


# ---------------------------------------------------------------------------
#                          PAPKALAR
# ---------------------------------------------------------------------------

@app.get("/api/folders")
async def list_folders():
    owner_id = auth.current_user_id()
    folders = db.fetchall(
        "SELECT * FROM folders WHERE owner_id = ? ORDER BY COALESCE(parent_id, ''), sort_order ASC, "
        "name COLLATE NOCASE ASC", (owner_id,))
    counts = db.fetchall("SELECT folder_id, COUNT(*) as n FROM videos WHERE owner_id = ? AND folder_id IS NOT NULL "
                         "GROUP BY folder_id", (owner_id,))
    count_by_id = {c["folder_id"]: c["n"] for c in counts}
    child_counts = db.fetchall("SELECT parent_id, COUNT(*) as n FROM folders WHERE owner_id = ? AND parent_id IS NOT NULL "
                               "GROUP BY parent_id", (owner_id,))
    child_count_by_id = {c["parent_id"]: c["n"] for c in child_counts}
    return [{"id": f["id"], "name": f["name"], "parent_id": f["parent_id"],
             "sort_order": f["sort_order"] or 0, "created_at": f["created_at"],
             "child_count": child_count_by_id.get(f["id"], 0),
             "video_count": count_by_id.get(f["id"], 0)} for f in folders]


@app.post("/api/folders")
async def create_folder(name: str = Form(...), parent_id: str = Form("")):
    owner_id = auth.current_user_id()
    name = name.strip()
    if not name:
        raise HTTPException(400, "Papka nomi bo'sh bo'lishi mumkin emas.")
    parent_id = parent_id.strip() or None
    if parent_id and not db.fetchone("SELECT id FROM folders WHERE id = ? AND owner_id = ?", (parent_id, owner_id)):
        raise HTTPException(404, "Asosiy papka topilmadi.")
    duplicate = db.fetchone("SELECT id FROM folders WHERE owner_id = ? AND parent_id IS ? AND name = ? COLLATE NOCASE",
                            (owner_id, parent_id, name))
    if duplicate:
        raise HTTPException(409, "Shu joyda bunday nomli papka mavjud.")
    last = db.fetchone("SELECT COALESCE(MAX(sort_order), -1) AS n FROM folders WHERE owner_id = ? AND parent_id IS ?",
                       (owner_id, parent_id))
    folder_id = db.new_id()
    sort_order = int(last["n"]) + 1
    db.execute("INSERT INTO folders (id, name, parent_id, sort_order, created_at, owner_id) VALUES (?, ?, ?, ?, ?, ?)",
               (folder_id, name, parent_id, sort_order, db.now(), owner_id))
    return {"id": folder_id, "name": name, "parent_id": parent_id, "sort_order": sort_order}


@app.post("/api/folders/{folder_id}/move")
async def move_folder(folder_id: str, direction: str = Form(...)):
    owner_id = auth.current_user_id()
    folder = db.fetchone("SELECT id, parent_id FROM folders WHERE id = ?", (folder_id,))
    if not folder:
        raise HTTPException(404, "Papka topilmadi.")
    if direction not in ("up", "down"):
        raise HTTPException(400, "Yo'nalish up yoki down bo'lishi kerak.")
    siblings = db.fetchall(
        "SELECT id FROM folders WHERE owner_id = ? AND parent_id IS ? ORDER BY sort_order ASC, name COLLATE NOCASE ASC",
        (owner_id, folder["parent_id"]))
    ids = [row["id"] for row in siblings]
    index = ids.index(folder_id)
    target = index - 1 if direction == "up" else index + 1
    if target < 0 or target >= len(ids):
        return {"ok": True}
    ids[index], ids[target] = ids[target], ids[index]
    for order, sibling_id in enumerate(ids):
        db.execute("UPDATE folders SET sort_order = ? WHERE id = ?", (order, sibling_id))
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str):
    f = db.fetchone("SELECT id, parent_id FROM folders WHERE id = ?", (folder_id,))
    if not f:
        raise HTTPException(404, "Papka topilmadi.")
    db.execute("UPDATE videos SET folder_id = ? WHERE folder_id = ?", (f["parent_id"], folder_id))
    db.execute("UPDATE folders SET parent_id = ? WHERE parent_id = ?", (f["parent_id"], folder_id))
    db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    return {"ok": True}


@app.post("/api/videos/{video_id}/folder")
async def set_video_folder(video_id: str, folder_id: str = Form("")):
    owner_id = auth.current_user_id()
    v = db.fetchone("SELECT id FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    folder_id = folder_id.strip() or None
    if folder_id:
        f = db.fetchone("SELECT id FROM folders WHERE id = ? AND owner_id = ?", (folder_id, owner_id))
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
        "SELECT id, chunk_index, start_time, end_time, status, attempts, error, transcript, language FROM chunks "
        "WHERE video_id = ? ORDER BY chunk_index ASC", (video_id,))
    logs = db.get_logs(video_id, 200)
    results = db.fetchall("SELECT id, kind, filename, created_at FROM results WHERE video_id = ?", (video_id,))
    out = video_public(v)
    # Yakuniy videolarning vaqt nuqtalari (slow/freeze) - pleyer video
    # almashtirilganda joriy vaqtni shu bilan o'tkazadi (faqat batafsil sahifada).
    out["timeline_points"] = transcription.active_timeline_points(_parse_points(v["freeze_points"]))
    out["track_timeline_points"] = {
        t["provider"]: transcription.active_timeline_points(_parse_points(t["freeze_points"]))
        for t in db.fetchall("SELECT provider, freeze_points FROM audio_tracks WHERE video_id = ?", (video_id,))}
    parsed_transcript_segments = _json_or_empty(v["transcript_segments"])
    out["chunks"] = [chunk_detail(c, parsed_transcript_segments) for c in chunks]
    out["logs"] = logs
    out["results"] = results
    out["transcript_text"] = v["transcript_text"] or ""
    out["translation_text"] = v["translation_text"] or ""
    out["learning_text"] = ""
    learning_source = db.fetchone(
        "SELECT srt_path FROM learning_tracks WHERE video_id = ? AND srt_status = 'uploaded'", (video_id,))
    if learning_source and learning_source["srt_path"] and Path(learning_source["srt_path"]).exists():
        try:
            learning_segments = translation.parse_srt_direct(
                Path(learning_source["srt_path"]).read_text(encoding="utf-8"))
            out["learning_text"] = "\n\n".join(s.get("text", "") for s in learning_segments)
        except (OSError, ValueError):
            pass
    out["learning_words"] = _learning_words_payload(video_id)
    out["expected_segment_count"] = len(chunks) if v["transcript_segments"] else None
    # "words" (olib tashlangan bo'lak so'zlari) faqat qaytarish uchun serverda kerak.
    out["flagged_issues"] = [{k: val for k, val in i.items() if k != "words"}
                             for i in (json.loads(v["flagged_issues"]) if v["flagged_issues"] else [])]
    out["stt_provider"] = v["stt_provider"]
    out["speaker_names"] = _json_or_none(v["speaker_names"]) or {}
    if v["tts_job_id"]:
        tj = db.fetchone("SELECT status, error, total_segments, completed_segments FROM tts_jobs WHERE id = ?",
                          (v["tts_job_id"],))
        out["tts_job"] = tj
    return out


@app.post("/api/videos/{video_id}/name")
async def rename_video(video_id: str, name: str = Form(...)):
    video = db.fetchone("SELECT id FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise HTTPException(404, "Video topilmadi.")
    name = name.strip()
    if not name:
        raise HTTPException(400, "Video nomi bo'sh bo'lishi mumkin emas.")
    if len(name) > 300:
        raise HTTPException(400, "Video nomi 300 belgidan oshmasligi kerak.")
    db.execute("UPDATE videos SET original_name = ?, updated_at = ? WHERE id = ?", (name, db.now(), video_id))
    return {"ok": True, "name": name}


@app.get("/api/videos/{video_id}/thumbnail")
async def get_thumbnail(video_id: str):
    v = db.fetchone("SELECT thumbnail_path FROM videos WHERE id = ?", (video_id,))
    if not v or not v["thumbnail_path"] or not Path(v["thumbnail_path"]).exists():
        raise HTTPException(404, "Thumbnail topilmadi.")
    return FileResponse(v["thumbnail_path"], media_type="image/jpeg")


@app.delete("/api/videos/{video_id}")
async def delete_video(video_id: str, mode: str = "full"):
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
        delete_video_completely(v)
    return {"ok": True}


def delete_video_completely(v: dict):
    """Kutubxona loyihasini barcha fayllari va yozuvlari bilan o'chiradi:
    asl video, bo'laklar, matn/tarjima/SRT natijalari, barcha TTS audiolari
    (asosiy, ikkinchi provayder va ruscha o'rganish treklari) va yakuniy videolar.
    Xarajatlar O'CHIRILMAYDI - hisobot uchun video nomi bilan saqlanib qoladi."""
    import tts as tts_module
    from storage import TTS_DIR
    video_id = v["id"]
    worker.CANCEL_FLAGS[video_id] = True
    worker.cleanup_learning_track(video_id, delete_record=True)
    job_ids = {v["tts_job_id"]} if v["tts_job_id"] else set()
    job_ids |= {r["id"] for r in db.fetchall("SELECT id FROM tts_jobs WHERE video_id = ?", (video_id,))}
    job_ids |= {r["tts_job_id"] for r in db.fetchall(
        "SELECT tts_job_id FROM audio_tracks WHERE video_id = ? AND tts_job_id IS NOT NULL", (video_id,))}
    for job_id in job_ids:
        tts_module.PAUSE_FLAGS.pop(job_id, None)
        tts_module.CANCEL_FLAGS[job_id] = True
        db.execute("DELETE FROM tts_segments WHERE job_id = ?", (job_id,))
        db.execute("DELETE FROM tts_jobs WHERE id = ?", (job_id,))
        shutil.rmtree(TTS_DIR / job_id, ignore_errors=True)
    for table in ("audio_tracks", "freeze_point_events", "chunks", "results", "job_logs"):
        db.execute(f"DELETE FROM {table} WHERE video_id = ?", (video_id,))
    db.execute("UPDATE costs SET video_name = ? WHERE video_id = ?", (v["original_name"], video_id))
    db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    for base in (VIDEOS_DIR, CHUNKS_DIR, RESULTS_DIR, SPLIT_DIR):
        shutil.rmtree(base / video_id, ignore_errors=True)


@app.post("/api/videos/{video_id}/restart")
async def restart_video_endpoint(video_id: str, _=Depends(check_admin)):
    """Loyihani video yuklangandan keyingi holatga qaytaradi - transkripsiya,
    tarjima, audio va yakuniy video (va ularga tegishli fayllar) o'chiriladi,
    original video saqlanib qoladi va bo'laklarga avtomatik qayta bo'linadi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    try:
        worker.restart_video(video_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/videos/{video_id}/restart/{stage}")
async def restart_stage_endpoint(video_id: str, stage: str, _=Depends(check_admin)):
    """Loyihani berilgan bosqichdan (transcription/translation/audio) boshlab
    qaytadan boshlaydi - undan oldingi ish saqlanadi, undan keyingisi tozalanadi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if stage not in ("transcription", "translation", "audio"):
        raise HTTPException(400, "Noto'g'ri bosqich nomi.")
    try:
        worker.reset_from_stage(video_id, stage)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.get("/api/admin/freeze-point-stats")
async def freeze_point_stats(days: int = 30, _=Depends(check_admin)):
    """Freeze-point mexanizmi (ustuvorlik zanjirining ENG OXIRGI, zaxira
    chorasi) qanchalik tez-tez ishga tushayotganini ko'rsatadi - agar ko'p
    bo'lsa, 0.85-1.20 tezlik byudjeti (SPEED_HARD_MIN/MAX) qayta ko'rib
    chiqilishi kerak degani."""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - max(days, 0) * 86400))
    events = db.fetchall(
        "SELECT * FROM freeze_point_events WHERE created_at >= ? ORDER BY created_at DESC LIMIT 500",
        (since,))
    # tts_segments'da created_at ustuni yo'q, shuning uchun umumiy nisbat
    # (freeze chastotasi) uchun jami tayyor segmentlar soni taqriban olinadi.
    total_completed_segments = db.fetchone(
        "SELECT COUNT(*) c FROM tts_segments WHERE status = 'completed'")["c"]
    return {
        "since": since, "days": days,
        "freeze_event_count": len(events),
        "total_completed_segments": total_completed_segments,
        "events": [dict(e) for e in events[:100]],
    }


@app.post("/api/videos/{video_id}/segment")
async def segment_video_endpoint(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] not in ("uploaded", "segmenting", "segments_ready"):
        raise HTTPException(400, f"Video holati '{v['status']}' - bo'laklarga bo'lish mumkin emas.")
    if not worker.enqueue_segment(video_id):
        raise HTTPException(409, "Video allaqachon bo'laklanmoqda.")
    return {"ok": True}


@app.post("/api/videos/{video_id}/transcribe")
async def transcribe_endpoint(video_id: str, language: str = Form(""), instruction: str = Form(""),
                                topic_group: str = Form(""), stt_provider: str = Form("openai"),
                                diarize: bool = Form(False), num_speakers: int = Form(0),
                                send_keyterms: bool = Form(True), send_video: bool = Form(False)):
    """Matn olish: stt_provider = "elevenlabs" (Scribe, bitta so'rov, bo'laklarga
    bo'lish shart emas) yoki "openai" (Whisper, 5 daqiqalik bo'laklar)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    _validate_transcribe_language(language)
    if stt_provider not in ("openai", "elevenlabs"):
        raise HTTPException(400, "Noma'lum matn olish provayderi.")
    if num_speakers and not (1 <= num_speakers <= 32):
        raise HTTPException(400, "Spikerlar soni 1 dan 32 gacha bo'lishi kerak.")
    if stt_provider == "elevenlabs":
        if v["status"] not in ("uploaded", "segments_ready", "transcription_ready", "cancelled"):
            raise HTTPException(400, f"Video holati '{v['status']}' - matn olishni boshlab bo'lmaydi.")
        if not keys_manager.has_any_active_key(provider="elevenlabs"):
            raise HTTPException(400, "ElevenLabs API kalit topilmadi. Sozlamalar -> API kalitlar bo'limida qo'shing.")
    else:
        if v["status"] not in ("segments_ready", "transcription_ready"):
            raise HTTPException(400, f"Video holati '{v['status']}' - transkripsiyani boshlab bo'lmaydi. "
                                      f"Avval videoni bo'laklarga bo'ling.")
        if not keys_manager.has_any_active_key():
            raise HTTPException(400, "Ishlaydigan OpenAI API kalit topilmadi. Avval API kalit qo'shing.")
        if not db.fetchone("SELECT 1 FROM chunks WHERE video_id = ? LIMIT 1", (video_id,)):
            raise HTTPException(400, "OpenAI Whisper uchun avval videoni bo'laklarga bo'ling.")
        db.execute("UPDATE chunks SET status = 'pending', transcript = NULL, language = NULL, force_split = 0 "
                   "WHERE video_id = ?", (video_id,))
    worker.start_transcription(video_id, language, instruction, topic_group, stt_provider=stt_provider,
                               diarize=diarize, num_speakers=num_speakers or None,
                               stt_options={"keyterms": send_keyterms, "send_video": send_video})
    return {"ok": True}


@app.post("/api/videos/{video_id}/transcript/restore-removed")
async def restore_removed_endpoint(video_id: str, issue_index: int = Form(...)):
    _ensure_video(video_id)
    try:
        return worker.restore_removed_segment(video_id, issue_index)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/videos/{video_id}/transcript/srt-upload")
async def upload_original_transcript_srt_endpoint(video_id: str, file: UploadFile = File(...),
                                                  language: str = Form("")):
    """Qurilmadan tayyor ORIGINAL SRT yuklab, Whisper bosqichini almashtiradi.

    SRT o'z vaqt belgilarini saqlaydi; foydalanuvchi keyingi ekranda matnni
    tekshiradi va odatdagi kabi alohida tasdiqlaydi.
    """
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["status"] not in ("uploaded", "segments_ready", "transcription_ready", "cancelled"):
        raise HTTPException(400, "Original SRT faqat matn olish bosqichida yuklanadi.")
    _validate_transcribe_language(language)
    name = file.filename or ""
    if not name.lower().endswith(".srt"):
        raise HTTPException(400, "Faqat .srt fayl qabul qilinadi.")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(400, "SRT fayl juda katta (limit: 10 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="ignore")
    try:
        parsed = translation.parse_srt_direct(text)
    except ValueError as e:
        raise HTTPException(400, str(e))

    ordered = sorted(parsed, key=lambda s: (s["start"], s["end"]))
    segments = []
    fixed_zero = 0
    for i, item in enumerate(ordered):
        start = float(item["start"])
        end = float(item["end"])
        if start < 0 or end < start:
            raise HTTPException(400, f"SRT'dagi {i + 1}-bo'lak vaqt belgisi noto'g'ri (manfiy davomiylik).")
        if end - start < stt_words.MIN_BLOCK_SEC:
            # 0 soniyalik blok (masalan 1:41:29 --> 1:41:29) - keyingi blokka tegmasdan cho'ziladi.
            limit = float(ordered[i + 1]["start"]) if i + 1 < len(ordered) else start + stt_words.MIN_BLOCK_SEC
            end = max(end, min(start + stt_words.MIN_BLOCK_SEC, limit))
            fixed_zero += 1
        seg = {"start": start, "end": end, "text": (item.get("text") or "").strip()}
        if item.get("speaker") is not None:
            seg["speaker"] = item["speaker"]
        segments.append(seg)
    segments, _, removed = stt_words.remove_hallucinations(
        segments, [], worker._hallucination_phrases(v["owner_id"]), transcription.detect_repetition)
    if not segments:
        raise HTTPException(400, "SRT'da matnli blok qolmadi.")

    duration = float(v["duration"] or 0)
    if duration and segments[-1]["end"] > duration + 5:
        raise HTTPException(
            400,
            f"SRT oxirgi vaqti ({segments[-1]['end']:.1f}s) video davomiyligidan "
            f"({duration:.1f}s) ancha uzun. Boshqa videoning SRT fayli tanlangan bo'lishi mumkin.",
        )

    txt_text = transcription.build_txt(segments)
    db.execute("UPDATE chunks SET status = 'completed', error = NULL WHERE video_id = ?", (video_id,))
    worker._update_video(
        video_id,
        status="transcription_ready",
        blocked_reason=None,
        progress=100,
        message="Original SRT yuklandi. Tekshirib tasdiqlang.",
        error=None,
        language=language,
        detected_language=language or None,
        transcript_text=txt_text,
        transcript_segments=json.dumps(segments, ensure_ascii=False),
        transcript_approved=0,
        transcript_words=None,
        stt_provider="srt",
        flagged_issues=json.dumps(removed, ensure_ascii=False),
        translation_status="none",
        translation_text="",
        translation_segments="[]",
    )
    worker.write_transcript_results(video_id)
    notes = []
    if fixed_zero:
        notes.append(f"{fixed_zero} ta 0 soniyalik blok tuzatildi")
    if removed:
        notes.append(f"uydirma deb {len(removed)} ta blok olib tashlandi (qaytarish mumkin)")
    speakers = transcription.speaker_count(segments)
    if speakers >= 2:
        notes.append(f"{speakers} ta spiker")
    db.log_line(video_id, f"Original SRT qurilmadan yuklandi: {name} ({len(segments)} ta segment"
                          f"{'; ' + '; '.join(notes) if notes else ''}).")
    return {"ok": True, "segment_count": len(segments), "removed_count": len(removed), "fixed_zero": fixed_zero,
            "speaker_count": speakers}


@app.get("/api/glossary/groups")
async def glossary_groups_endpoint():
    return {"groups": glossary_data.GLOSSARY_GROUPS}


@app.get("/api/transcribe-languages")
async def transcribe_languages_endpoint():
    """Video -> Matn bosqichida (va qayta yuborish modalida) tanlash mumkin
    bo'lgan til ro'yxati - frontend va backend bitta manbadan (storage.py)
    foydalanishi uchun."""
    return {"languages": [{"code": code, "label": label} for code, label in TRANSCRIBE_LANGUAGES]}


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


@app.get("/api/videos/{video_id}/audio-range")
async def audio_range_endpoint(video_id: str, start: float, end: float):
    """Original videodan istalgan [start,end] vaqt oralig'idagi audioni qaytaradi -
    tarjima blok muharriridagi 'tinglash' tugmasi uchun (bitta yakuniy blok bir
    nechta original segmentni qamrab olishi mumkin, shuning uchun segment indeksi
    emas, vaqt oralig'i ishlatiladi)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["path"] or not Path(v["path"]).exists():
        raise HTTPException(404, "Original video fayli topilmadi.")
    if end <= start or start < 0:
        raise HTTPException(400, "Vaqt oralig'i noto'g'ri.")
    clip_dir = CHUNKS_DIR / video_id / "listen"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_path = clip_dir / f"range_{int(start * 1000):09d}_{int(end * 1000):09d}.mp3"
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None, transcription.extract_audio_slice, Path(v["path"]), start, end, clip_path)
    except Exception as e:
        raise HTTPException(500, f"Audio ajratishda xato: {e}")
    return FileResponse(clip_path, media_type="audio/mpeg")


@app.post("/api/videos/{video_id}/transcript/segments/{index}/retranscribe")
async def retranscribe_segment_endpoint(video_id: str, index: int, language: str = Form(None)):
    """Bitta aniq segmentni original videodan qayta ajratib, qayta Whisper'ga yuboradi -
    butun bo'lakni emas, faqat shu bitta segmentni. Mos tarjima bo'lagi ham tozalanadi
    (qayta tarjima qilinishi kerakligini bildirish uchun). `language` berilsa (None emas),
    aynan shu til so'rovga majburiy yuboriladi - berilmasa, videoning umumiy tili
    ishlatiladi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if language is not None:
        _validate_transcribe_language(language)
    try:
        result = await worker.retranscribe_segment(video_id, index, language)
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
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND kind = 'file' AND owner_id = ?",
                    (cloud_file_id, auth.current_user_id()))
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
    # Bo'laklar soni endi YAKUNIY tarjima bloklariga (translation_segments) mos
    # bo'lishi kerak - "Tahrirlash va audio" jadvali bir necha original segmentni
    # birlashtirgan bo'lishi mumkin, shuning uchun original transkripsiya emas.
    translations = _json_or_empty(v["translation_segments"])
    if not translations:
        raise HTTPException(400, "Avval tarjima tayyor bo'lishi kerak.")
    try:
        texts = translation.parse_manual_translation(text, translations)
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
    if not current_translations:
        raise HTTPException(400, "Avval tarjima tayyor bo'lishi kerak.")

    try:
        target_indices = sorted(set(int(x.strip()) - 1 for x in indices.split(",") if x.strip()))
    except ValueError:
        raise HTTPException(400, "Segment raqamlarini vergul bilan ajratib kiriting (masalan: 3, 15, 42).")
    if not target_indices:
        raise HTTPException(400, "Kamida bitta segment raqami kiriting.")

    # Har bir so'ralgan ORIGINAL segment indeksi qaysi YAKUNIY blokka tegishli
    # ekanini aniqlaymiz. Agar shu blok bir nechta original segmentni birlashtirgan
    # bo'lsa (mexanik bo'lingan bitta gap), bitta segmentning vaqt belgisi bo'yicha
    # qaysi qismini almashtirish kerakligi noaniq bo'lib qoladi - shunday holatda
    # aniq xato qaytariladi va hech narsa o'zgartirilmaydi.
    block_by_source_index = {}
    for bi, block in enumerate(current_translations):
        src = block.get("source_indices") or [bi]
        for x in src:
            block_by_source_index[x] = (bi, len(src))

    merged_conflicts = sorted(
        idx for idx in target_indices
        if block_by_source_index.get(idx) and block_by_source_index[idx][1] > 1
    )
    if merged_conflicts:
        raise HTTPException(400, {
            "message": (f"{', '.join(str(i + 1) for i in merged_conflicts)} - segment(lar) boshqa segment(lar) "
                        "bilan bitta yakuniy blokka birlashtirilgan - bu yerdan alohida tuzatib bo'lmaydi. "
                        "\"Tahrirlash va audio\" bo'limidan yakuniy blokni tahrirlang."),
        })

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

    new_block_texts = [b.get("text") or "" for b in current_translations]
    for idx, text in matched.items():
        bi, _ = block_by_source_index[idx]
        new_block_texts[bi] = text

    try:
        result = worker.apply_block_edits(video_id, new_block_texts)
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


@app.get("/api/videos/{video_id}/tracks")
async def video_tracks_endpoint(video_id: str):
    """Video 'completed' bo'lgach ikkinchi provayder bilan yaratilgan
    qo'shimcha audio/video track(lar)ini qaytaradi (frontend pleyer va
    Botga yuborish tanlovi uchun)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    rows = db.fetchall("SELECT provider, audio_status, final_video_status, subtitled_video_status, "
                        "subtitled_video_error, error, updated_at FROM audio_tracks WHERE video_id = ? "
                        "ORDER BY created_at ASC", (video_id,))
    return [dict(r) for r in rows]


@app.post("/api/videos/{video_id}/audio/track")
async def create_audio_track_endpoint(
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
    """Asosiy video 'completed' bo'lgach, IKKINCHI provayder bilan qo'shimcha
    audio+video yaratishni boshlaydi - asosiy natijaga tegmaydi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    segments = _json_or_empty(v["translation_segments"])
    empty_indices = [i for i, s in enumerate(segments) if not (s.get("text") or "").strip()]
    if empty_indices and not skip_empty:
        raise HTTPException(409, {"kind": "empty_segments", "count": len(empty_indices),
                                   "indices": [i + 1 for i in empty_indices[:20]]})
    if provider == "aisha" and not aisha_key.strip():
        raise HTTPException(400, "Aisha API kalit kiritilmagan.")
    if provider == "openai" and not keys_manager.has_any_active_key():
        raise HTTPException(400, "Ishlaydigan OpenAI API kalit topilmadi. Avval API kalit qo'shing.")
    try:
        job_id = worker.start_secondary_track(video_id, provider, voice, mood, speed, instructions,
                                               aisha_key.strip(), stretch_to_fit)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "tts_job_id": job_id}


@app.get("/api/videos/{video_id}/learning")
async def learning_track_endpoint(video_id: str):
    """"Ruscha o'rganish" trekining joriy holatini qaytaradi (frontend polling
    uchun) - Uzbek pipeline holatidan mustaqil."""
    v = db.fetchone("SELECT id FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    t = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    return dict(t) if t else None


@app.post("/api/videos/{video_id}/learning/srt")
async def upload_learning_srt_endpoint(video_id: str, file: UploadFile = File(...)):
    """Foydalanuvchi qo'lda tayyorlagan Learning SRT faylni yuklaydi. Dastur bu
    faylni YARATMAYDI - faqat validatsiya qilib, o'zgartirmasdan saqlaydi.
    Mixed-language (o'zbek lotin + rus kirill) matn NORMAL - hech qanday
    tozalash/transliteratsiya/tarjima qilinmaydi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    name = (file.filename or "").lower()
    if not name.endswith(".srt"):
        raise HTTPException(400, "Faqat .srt fayl qabul qilinadi.")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(400, "Learning SRT juda katta (limit: 10 MB).")
    if not raw.strip():
        raise HTTPException(400, "Yuklangan fayl bo'sh.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Fayl UTF-8 kodlashda emas. Iltimos, UTF-8 formatida saqlab qayta yuklang.")
    try:
        segments = translation.parse_srt_direct(text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        worker.apply_learning_srt(video_id, text, file.filename or "learning.srt", len(segments))
    except translation.LearningSrtError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "segment_count": len(segments)}


def _learning_segments(video_id: str) -> tuple[dict, list]:
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["srt_status"] != "uploaded" or not track["srt_path"]:
        raise HTTPException(404, "Ruscha o'rganish matni topilmadi.")
    path = Path(track["srt_path"])
    if not path.exists():
        raise HTTPException(404, "Ruscha o'rganish SRT fayli topilmadi.")
    try:
        segments = translation.parse_srt_direct(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"Ruscha o'rganish matnini o'qib bo'lmadi: {e}")
    return dict(track), segments


def _save_learning_segments(video_id: str, segments: list, filename: str):
    if not segments:
        raise HTTPException(400, "Kamida bitta matn bo'lagi kerak.")
    srt_text = transcription.build_srt(segments)
    _apply_learning_srt_text(video_id, srt_text, filename, len(segments))


def _apply_learning_srt_text(video_id: str, srt_text: str, filename: str, segment_count: int):
    try:
        worker.apply_learning_srt(video_id, srt_text, filename, segment_count)
    except translation.LearningSrtError as e:
        raise HTTPException(400, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


def _manual_learning_segments(v: dict, text: str) -> list:
    source_segments = _json_or_empty(v["transcript_segments"])
    if not source_segments:
        raise HTTPException(400, "Original matn segmentlari topilmadi.")
    try:
        texts = translation.parse_manual_translation(text, source_segments)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return [
        {"start": source_segments[i]["start"], "end": source_segments[i]["end"], "text": value}
        for i, value in enumerate(texts)
    ]


@app.get("/api/videos/{video_id}/learning/blocks")
async def learning_blocks_endpoint(video_id: str):
    _, segments = _learning_segments(video_id)
    return [
        {"index": i, "start": s["start"], "end": s["end"], "text": s.get("text", "")}
        for i, s in enumerate(segments)
    ]


@app.post("/api/videos/{video_id}/learning/text")
async def learning_manual_text_endpoint(video_id: str, text: str = Form(...)):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    segments = _manual_learning_segments(v, text)
    _save_learning_segments(video_id, segments, "ruscha-organish.srt")
    return {"ok": True, "segment_count": len(segments)}


@app.post("/api/videos/{video_id}/learning/file")
async def learning_manual_file_endpoint(video_id: str, file: UploadFile = File(...)):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["transcript_approved"]:
        raise HTTPException(400, "Avval original matnni tasdiqlang.")
    raw = await file.read()
    name = (file.filename or "ruscha-organish.txt").lower()
    if name.endswith(".docx"):
        text = _extract_docx_text(raw)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", errors="ignore")
    if name.endswith(".srt"):
        try:
            segments = translation.parse_srt_direct(text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        # Xom SRT saqlanadi - vaqt qatoridagi [yangi:..]/[takror:..] teglari yo'qolmasligi uchun.
        _apply_learning_srt_text(video_id, text, file.filename or "ruscha-organish.srt", len(segments))
        return {"ok": True, "segment_count": len(segments)}
    segments = _manual_learning_segments(v, text)
    _save_learning_segments(video_id, segments, file.filename or "ruscha-organish.srt")
    return {"ok": True, "segment_count": len(segments)}


@app.post("/api/videos/{video_id}/learning/save-blocks")
async def learning_save_blocks_endpoint(video_id: str, payload: dict):
    track, segments = _learning_segments(video_id)
    texts = payload.get("texts")
    if not isinstance(texts, list):
        raise HTTPException(400, "'texts' massiv bo'lishi kerak.")
    if len(texts) != len(segments):
        raise HTTPException(400, f"Bo'laklar soni mos emas: {len(texts)} / {len(segments)}.")
    updated = [
        {"start": s["start"], "end": s["end"], "text": str(texts[i]).strip()}
        for i, s in enumerate(segments)
    ]
    if not any(u["text"] for u in updated):
        raise HTTPException(400, "Kamida bitta matn bo'lagi kerak.")
    # Faqat matn qatorlari almashtiriladi - vaqt qatoridagi so'z teglari saqlanadi.
    original = Path(track["srt_path"]).read_text(encoding="utf-8")
    srt_text = translation.replace_srt_block_texts(original, [u["text"] for u in updated])
    _apply_learning_srt_text(video_id, srt_text, track.get("srt_filename") or "ruscha-organish.srt",
                             sum(1 for u in updated if u["text"]))
    return {"ok": True, "changed_count": sum(
        1 for i, s in enumerate(segments) if (s.get("text") or "") != updated[i]["text"])}


@app.post("/api/videos/{video_id}/learning/reset")
async def learning_reset_endpoint(video_id: str):
    v = db.fetchone("SELECT id FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    worker.cleanup_learning_track(video_id, delete_record=True)
    db.log_line(video_id, "Ruscha o'rganish matni va hosila fayllari qaytadan boshlash uchun tozalandi.")
    return {"ok": True}


@app.get("/api/videos/{video_id}/learning/srt-download")
async def download_learning_srt(video_id: str):
    track = db.fetchone("SELECT srt_path, srt_filename FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or not track["srt_path"] or not Path(track["srt_path"]).exists():
        raise HTTPException(404, "Learning SRT topilmadi.")
    return FileResponse(track["srt_path"], media_type="application/x-subrip",
                        filename=track["srt_filename"] or "learning.srt")


@app.get("/api/videos/{video_id}/learning/audio-download")
async def download_learning_audio(video_id: str, request: Request):
    track = db.fetchone("SELECT audio_path, audio_status FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["audio_status"] != "ready" or not track["audio_path"]:
        raise HTTPException(404, "Learning audio topilmadi.")
    path = Path(track["audio_path"])
    if not path.exists():
        raise HTTPException(404, "Learning audio topilmadi.")
    return range_file_response(request, path, "audio/wav")


@app.post("/api/videos/{video_id}/learning/audio")
async def start_learning_audio_endpoint(
    video_id: str, provider: str = Form(...), voice: str = Form(""), mood: str = Form(""),
    speed: float = Form(1.0), instructions: str = Form(""), aisha_key: str = Form(""),
    stretch_to_fit: bool = Form(True),
):
    """Yuklangan Learning SRT asosida, MAVJUD TTS mexanizmi orqali mustaqil
    Learning audio yaratishni boshlaydi - asosiy Uzbek audioga tegmaydi."""
    v = db.fetchone("SELECT id FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if provider == "aisha" and not aisha_key.strip():
        raise HTTPException(400, "Aisha API kalit kiritilmagan.")
    if provider == "openai" and not keys_manager.has_any_active_key():
        raise HTTPException(400, "Ishlaydigan OpenAI API kalit topilmadi. Avval API kalit qo'shing.")
    try:
        job_id = worker.start_learning_track(video_id, provider, voice, mood, speed, instructions,
                                              aisha_key.strip(), stretch_to_fit)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "tts_job_id": job_id}


@app.post("/api/videos/{video_id}/learning/render")
async def learning_render_endpoint(video_id: str):
    """Learning yakuniy videoni qo'lda qayta yig'ish (odatda audio tayyor
    bo'lgach avtomatik ishga tushadi - bu asosan "qayta urinish" uchun)."""
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["audio_status"] != "ready" or not track["audio_path"]:
        raise HTTPException(400, "Avval Learning audio tayyor bo'lishi kerak.")
    if not worker.enqueue_learning_render(video_id):
        raise HTTPException(409, "Learning video allaqachon yig'ilmoqda.")
    return {"ok": True}


@app.get("/api/videos/{video_id}/learning/final-download")
async def download_learning_final(video_id: str, request: Request):
    """Tayyor Russian Learning yakuniy videoni yuklab olish - asosiy Uzbek
    final-download'dan mustaqil, alohida fayl."""
    track = db.fetchone("SELECT final_video_path, final_video_status FROM learning_tracks WHERE video_id = ?",
                         (video_id,))
    if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
        raise HTTPException(404, "Learning video topilmadi.")
    path = Path(track["final_video_path"])
    if not path.exists():
        raise HTTPException(404, "Learning video topilmadi.")
    return range_file_response(request, path, "video/mp4")


# ---------------------------------------------------------------------------
#     Learning so'zlari, so'zlar treki, yuklab olinadigan Learning videosi va intro
# ---------------------------------------------------------------------------

def _learning_track_or_404(video_id: str) -> dict:
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["srt_status"] != "uploaded":
        raise HTTPException(404, "Learning SRT topilmadi.")
    return track


def _learning_asos(track: dict, video_id: str) -> str:
    v = db.fetchone("SELECT original_name FROM videos WHERE id = ?", (video_id,))
    fallback = Path(v["original_name"]).stem if v and v["original_name"] else "learning"
    return learning.asos_name(track["srt_filename"], fallback)


def _attachment(response, filename: str):
    response.headers["Content-Disposition"] = learning.content_disposition(filename)
    return response


def _learning_words_payload(video_id: str):
    track = db.fetchone("SELECT words_json, warnings_json, intro_slides_json FROM learning_tracks "
                        "WHERE video_id = ? AND srt_status = 'uploaded'", (video_id,))
    if not track:
        return None
    blocks = worker.learning_blocks(track)
    lists = translation.learning_word_lists(blocks)
    return {
        "new": lists["new"], "repeat": lists["repeat"],
        "new_count": len(lists["new"]), "repeat_count": len(lists["repeat"]),
        "tagged_blocks": sum(1 for b in blocks if b["words"]),
        "warnings": _json_or_empty(track["warnings_json"]),
        "intro_slides": _json_or_empty(track["intro_slides_json"]),
    }


def _intro_offset(track: dict, intro: bool) -> float:
    if not intro:
        return 0.0
    offset = worker.learning_intro_offset(track)
    if offset <= 0:
        raise HTTPException(404, "Intro hali yaratilmagan.")
    return offset


@app.get("/api/videos/{video_id}/learning/words")
async def learning_words_endpoint(video_id: str):
    payload = _learning_words_payload(video_id)
    if payload is None:
        raise HTTPException(404, "Learning SRT topilmadi.")
    return payload


@app.get("/api/videos/{video_id}/learning/words.vtt")
async def learning_words_vtt(video_id: str, intro: bool = False, download: bool = False):
    """So'zlar treki: har tegli blok vaqtida LEMMA — MA'NO (yuqori o'ng burchak).
    intro=1 - intro bilan yig'ilgan video uchun (freeze-point, so'ng intro surilishi)."""
    track = _learning_track_or_404(video_id)
    cues = learning.words_cues(worker.learning_blocks(track), worker.learning_freeze_points(track),
                               _intro_offset(track, intro))
    resp = Response(content=learning.build_words_vtt(cues), media_type="text/vtt; charset=utf-8")
    if download:
        _attachment(resp, f"{_learning_asos(track, video_id)}_sozlar{'_intro' if intro else ''}.vtt")
    return resp


@app.get("/api/videos/{video_id}/learning/subtitles.vtt")
async def learning_subtitles_vtt(video_id: str, intro: bool = False):
    """Learning subtitrlari (pleyer treki) - so'zlar treki bilan bir xil vaqt manbasi."""
    track = _learning_track_or_404(video_id)
    path = Path(track["srt_path"] or "")
    if not path.exists():
        raise HTTPException(404, "Learning SRT fayli topilmadi.")
    segments = translation.parse_srt_direct(path.read_text(encoding="utf-8"))
    segments = learning.shifted_segments(segments, worker.learning_freeze_points(track), _intro_offset(track, intro))
    return Response(content=transcription.build_vtt(segments), media_type="text/vtt; charset=utf-8")


@app.get("/api/videos/{video_id}/learning/subtitles-intro.srt")
async def learning_subtitles_intro_srt(video_id: str):
    """Intro bilan yig'ilgan video uchun surilgan Learning SRT."""
    track = _learning_track_or_404(video_id)
    path = Path(track["srt_path"] or "")
    if not path.exists():
        raise HTTPException(404, "Learning SRT fayli topilmadi.")
    segments = translation.parse_srt_direct(path.read_text(encoding="utf-8"))
    segments = learning.shifted_segments(segments, worker.learning_freeze_points(track), _intro_offset(track, True))
    resp = Response(content=transcription.build_srt(segments), media_type="application/x-subrip")
    return _attachment(resp, f"{_learning_asos(track, video_id)}_learning_intro.srt")


@app.post("/api/videos/{video_id}/learning/export")
async def learning_export_endpoint(video_id: str):
    track = _learning_track_or_404(video_id)
    if track["final_video_status"] != "ready":
        raise HTTPException(400, "Avval Learning videosi tayyor bo'lishi kerak.")
    if track["export_status"] == "generating":
        raise HTTPException(409, "Learning videosi allaqachon yig'ilmoqda.")
    worker.enqueue_learning_export(video_id)
    return {"ok": True}


@app.get("/api/videos/{video_id}/learning/video-download")
async def learning_video_download(video_id: str, request: Request):
    """ASOS_learning.mp4 - so'zlar kadrga yozilgan (va intro bo'lsa, intro bilan)
    versiya. Learning SRT'da so'zlar bo'lsa toza nusxaga qaytmaydi: foydalanuvchi
    faqat so'zlari o'chirib bo'lmaydigan tayyor eksportni oladi."""
    track = _learning_track_or_404(video_id)
    path = None
    if track["export_status"] == "ready" and track["export_video_path"]:
        path = Path(track["export_video_path"])
    elif any(b.get("words") for b in worker.learning_blocks(track)):
        raise HTTPException(409, "So'zlar videoga yozilmoqda. Tayyor bo'lgach qayta urinib ko'ring.")
    elif track["final_video_status"] == "ready" and track["final_video_path"]:
        path = Path(track["final_video_path"])
    if not path or not path.exists():
        raise HTTPException(404, "Learning video topilmadi.")
    return _attachment(range_file_response(request, path, "video/mp4"),
                       f"{_learning_asos(track, video_id)}_learning.mp4")


@app.post("/api/videos/{video_id}/learning/intro")
async def learning_intro_endpoint(video_id: str, strip_stress: bool = Form(False)):
    track = _learning_track_or_404(video_id)
    if track["final_video_status"] != "ready":
        raise HTTPException(400, "Intro Learning videosi parametrlari bilan yaratiladi - avval Learning "
                                 "videosi tayyor bo'lishi kerak.")
    lists = translation.learning_word_lists(worker.learning_blocks(track))
    if not lists["new"] and not lists["repeat"]:
        raise HTTPException(400, "Learning SRT'da yangi yoki takror so'z teglari yo'q - intro yaratilmaydi.")
    owner = db.fetchone("SELECT owner_id FROM videos WHERE id = ?", (video_id,))
    if lists["new"] and not keys_manager.has_any_active_key(owner_id=owner["owner_id"] if owner else None):
        raise HTTPException(400, "Ishlaydigan OpenAI API kalit topilmadi (intro ovozlari uchun).")
    if not worker.enqueue_learning_intro(video_id, strip_stress):
        raise HTTPException(409, "Intro allaqachon yaratilmoqda.")
    return {"ok": True}


@app.get("/api/videos/{video_id}/learning/intro-download")
async def learning_intro_download(video_id: str, request: Request):
    track = _learning_track_or_404(video_id)
    path = Path(track["intro_video_path"] or "")
    if track["intro_status"] != "ready" or not path.exists():
        raise HTTPException(404, "Intro topilmadi.")
    return _attachment(range_file_response(request, path, "video/mp4"),
                       f"{_learning_asos(track, video_id)}_intro.mp4")


@app.get("/api/videos/{video_id}/learning/intro-slides/{name}")
async def learning_intro_slide(video_id: str, name: str):
    if not re.fullmatch(r"\d{2,3}\.png", name):
        raise HTTPException(404, "Slayd topilmadi.")
    path = RESULTS_DIR / video_id / "learning_intro_slides" / name
    if not path.exists():
        raise HTTPException(404, "Slayd topilmadi.")
    return FileResponse(path, media_type="image/png")


@app.post("/api/videos/{video_id}/render")
async def render_endpoint(video_id: str):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    # "video_rendering" ham ruxsat etiladi - bu avvalgi urinish xato bilan
    # to'xtagan holat ham bo'lishi mumkin ("Qayta urinish" tugmasi shu holatdan
    # chaqiradi); ikkilanib ishga tushishning oldi enqueue_render() ichida
    # (faol, blocked_reason'siz "video_rendering" bo'lsa) olinadi.
    if v["status"] not in ("audio_ready", "completed", "video_rendering") or not v["audio_path"]:
        raise HTTPException(400, "Avval audio tayyor bo'lishi kerak.")
    if not worker.enqueue_render(video_id):
        raise HTTPException(409, "Video allaqachon yig'ilmoqda.")
    return {"ok": True}


@app.post("/api/videos/{video_id}/audio/remerge")
async def remerge_audio_endpoint(video_id: str, stretch_to_fit: bool = Form(True)):
    """Audio segmentlarini TTS orqali QAYTA YARATMASDAN (hech qanday API xarajat
    qilinmaydi), faqat ularni "kerak bo'lgandagina siqish" sozlamasi
    o'zgartirilgan holda QAYTA BIRLASHTIRADI. Buni asosan avval
    stretch_to_fit=false bilan yaratilgan, natijada segmentlar bir-birining
    ustiga tushib (overlap) gaplar oxiri kesilib qolgan eski audiolarni,
    pulsiz tuzatish uchun ishlatiladi. Segmentlarning audio fayllari diskda
    saqlanib qolgani uchun bu ishlaydi - TTS ish tugagach ular tozalanmaydi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if not v["tts_job_id"]:
        raise HTTPException(400, "Bu video uchun audio ishi topilmadi.")
    job = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (v["tts_job_id"],))
    if not job:
        raise HTTPException(404, "Audio ishi topilmadi.")
    if job["status"] not in ("completed", "error"):
        raise HTTPException(409, "Audio ish hozir band (ishlamoqda yoki navbatda) - biroz kuting.")
    db.execute("UPDATE tts_jobs SET stretch_to_fit = ? WHERE id = ?",
               (1 if stretch_to_fit else 0, job["id"]))
    tts.resume_job(job["id"])
    return {"ok": True}


@app.post("/api/videos/{video_id}/subtitle-burn")
async def subtitle_burn_endpoint(video_id: str, provider: str = Form(None)):
    """Yakuniy o'zbekcha videoga subtitr "kuydiradi" (hardsub) - ixtiyoriy,
    asosiy (provider=None) yoki qo'shimcha provayder treki uchun. Eski
    (avvaldan 'completed') videolar uchun ham, yangilari uchun ham bir xil
    ishlaydi - faqat video 'completed' va tegishli yakuniy video tayyor
    bo'lishi shart. Asosiy/qo'shimcha final_video_path'larga tegmaydi -
    YANGI, alohida fayl yaratiladi."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if provider != "learning" and v["status"] != "completed":
        raise HTTPException(400, "Avval yakuniy video tayyor bo'lishi kerak.")
    if provider == "learning":
        track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            raise HTTPException(400, "Ruscha o'rganish videosi hali tayyor emas.")
    elif provider:
        track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?", (video_id, provider))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            raise HTTPException(400, "Bu provayder uchun yakuniy video hali tayyor emas.")
    elif not v["final_video_path"]:
        raise HTTPException(400, "Yakuniy video topilmadi.")
    if not worker.enqueue_subtitle_burn(video_id, provider or None):
        raise HTTPException(409, "Subtitrli video allaqachon yaratilmoqda.")
    return {"ok": True}


@app.get("/api/videos/{video_id}/subtitled-download")
async def download_subtitled_video(video_id: str, request: Request, provider: str = None):
    v = _ensure_video(video_id)
    if provider == "learning":
        track = db.fetchone(
            "SELECT subtitled_video_path, subtitled_video_status FROM learning_tracks WHERE video_id = ?",
            (video_id,))
        if not track or track["subtitled_video_status"] != "ready" or not track["subtitled_video_path"]:
            raise HTTPException(404, "Ruscha o'rganish subtitrli videosi topilmadi.")
        video_path = track["subtitled_video_path"]
    elif provider:
        track = db.fetchone(
            "SELECT subtitled_video_path, subtitled_video_status FROM audio_tracks WHERE video_id = ? AND provider = ?",
            (video_id, provider))
        if not track or track["subtitled_video_status"] != "ready" or not track["subtitled_video_path"]:
            raise HTTPException(404, "Bu provayder uchun subtitrli video topilmadi.")
        video_path = track["subtitled_video_path"]
    else:
        if v["subtitled_video_status"] != "ready" or not v["subtitled_video_path"]:
            raise HTTPException(404, "Subtitrli video topilmadi.")
        video_path = v["subtitled_video_path"]
    if not video_path or not Path(video_path).exists():
        raise HTTPException(404, "Subtitrli video topilmadi.")
    return range_file_response(request, Path(video_path), "video/mp4")


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
        await _send_parts_to_telegram(video_id, parts, title, delete_after_send=True)
        _mark_telegram_sent(video_id, len(parts))
    except Exception as e:
        _mark_telegram_error(video_id, e)
    finally:
        if split_dir.exists():
            shutil.rmtree(split_dir, ignore_errors=True)


async def _send_parts_to_telegram(video_id: str, parts: list, title: str, start: int = 1,
                                  delete_after_send: bool = False):
    """Tayyor qismlarni ketma-ket yuboradi. `start` - shu qismdan boshlab
    (oldingi urinish o'rtada uzilgan bo'lsa, yuborilganlarini qayta yubormaslik
    uchun)."""
    total = len(parts)
    db.execute("UPDATE videos SET split_total_parts = ?, split_parts_sent = ? WHERE id = ?",
               (total, start - 1, video_id))
    async with httpx.AsyncClient(timeout=1200) as client:
        for i, part in enumerate(parts, start=1):
            if i < start:
                continue
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
            if delete_after_send and total > 1:
                Path(part).unlink(missing_ok=True)


def _mark_telegram_sent(video_id: str, total: int):
    db.execute("UPDATE videos SET telegram_send_status = 'sent', telegram_send_error = NULL WHERE id = ?",
               (video_id,))
    db.log_line(video_id, f"Video Telegram botga muvaffaqiyatli yuborildi ({total} qism).")


def _mark_telegram_error(video_id: str, error):
    db.execute("UPDATE videos SET telegram_send_status = 'error', telegram_send_error = ? WHERE id = ?",
               (str(error)[:500], video_id))
    db.log_line(video_id, f"Telegram botga yuborishda xato: {error}")


# "Video bo'lish": bir vaqtda faqat bitta katta video bo'linadi (disk va I/O).
_SPLIT_LOCK = asyncio.Lock()


async def _prepare_split_video(video_id: str):
    """'Video bo'lish'ga yuklangan videoni qismlarga bo'lib, qismlarni
    "Botga jo'natish" bosilguncha SPLIT_DIR'da saqlaydi. Telegram'ga hech narsa
    yubormaydi. Xatoda None qaytaradi (holat bazaga yoziladi)."""
    split_dir = SPLIT_DIR / video_id
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        return None
    if not (v["path"] and Path(v["path"]).exists()):
        # Asl video bo'lingach o'chirilgan - mavjud qismlar bilan ishlaymiz.
        parts = _ready_split_parts({**v, "split_status": "ready"})
        if parts:
            db.execute("UPDATE videos SET split_status = 'ready' WHERE id = ?", (video_id,))
            return parts
        db.execute("UPDATE videos SET split_status = 'error', split_error = ? WHERE id = ?",
                   ("Asl video ham, qismlar ham serverda topilmadi.", video_id))
        return None
    db.execute("UPDATE videos SET split_status = 'splitting', split_error = NULL, split_total_parts = 0, "
               "split_parts_sent = 0 WHERE id = ?", (video_id,))
    try:
        async with _SPLIT_LOCK:
            v = db.fetchone("SELECT path FROM videos WHERE id = ?", (video_id,))
            if not v:
                return None
            shutil.rmtree(split_dir, ignore_errors=True)
            parts = await asyncio.get_event_loop().run_in_executor(
                None, transcription.split_video_by_size, Path(v["path"]), split_dir)
    except Exception as e:
        shutil.rmtree(split_dir, ignore_errors=True)
        db.execute("UPDATE videos SET split_status = 'error', split_error = ? WHERE id = ?", (str(e)[:500], video_id))
        db.log_line(video_id, f"Videoni bo'lishda xato: {e}")
        return None
    db.execute("UPDATE videos SET split_status = 'ready', split_total_parts = ? WHERE id = ?", (len(parts), video_id))
    db.log_line(video_id, f"Video {len(parts)} qismga bo'lindi - botga jo'natishga tayyor.")
    if len(parts) > 1:
        # Joy tejash: endi video qismlarda saqlanadi. Kerak bo'lsa "Asliga
        # qaytarish" qismlarni qayta bitta faylga yig'adi.
        Path(v["path"]).unlink(missing_ok=True)
        db.log_line(video_id, "Asl video o'chirildi (qismlar saqlanadi) - kerak bo'lsa \"Asliga qaytarish\".")
    return parts


async def _restore_split_video(video_id: str):
    """Qismlarni qayta asl videoga yig'adi. split_restore_target='library' bo'lsa,
    keyin videoni Kutubxonaga (tarjima quvuriga) o'tkazadi."""
    split_dir = SPLIT_DIR / video_id
    try:
        async with _SPLIT_LOCK:
            v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
            if not v:
                return
            original = Path(v["path"])
            if not original.exists():
                parts = _ready_split_parts({**v, "split_status": "ready"})
                if not parts:
                    raise RuntimeError("Qismlar serverda topilmadi - videoni tiklab bo'lmaydi.")
                await asyncio.get_event_loop().run_in_executor(
                    None, transcription.join_video_parts, parts, original, v["duration"] or 0)
                db.log_line(video_id, "Video qismlardan asliga qaytarildi.")
    except Exception as e:
        reason = str(e)
        if len(reason) > 400:  # ffmpeg chiqishining oxiri - haqiqiy xato o'sha yerda
            reason = "..." + reason[-400:]
        db.execute("UPDATE videos SET split_status = 'ready', split_restore_target = NULL, split_error = ? "
                   "WHERE id = ?", (f"Asliga qaytarib bo'lmadi: {reason}", video_id))
        db.log_line(video_id, f"Asliga qaytarishda xato: {e}")
        return

    shutil.rmtree(split_dir, ignore_errors=True)
    if v["split_restore_target"] == "library":
        db.execute("UPDATE videos SET kind = 'pipeline', status = 'uploaded', message = ?, split_status = 'none', "
                   "split_error = NULL, split_total_parts = 0, split_parts_sent = 0, split_restore_target = NULL, "
                   "telegram_send_status = 'none', telegram_send_error = NULL, updated_at = ? WHERE id = ?",
                   ("Video bo'lishdan ko'chirildi. Bo'laklarga avtomatik bo'linmoqda...", db.now(), video_id))
        db.log_line(video_id, "Video \"Video bo'lish\"dan Kutubxonaga ko'chirildi.")
        worker.enqueue_segment(video_id)
    else:
        db.execute("UPDATE videos SET split_status = 'none', split_error = NULL, split_total_parts = 0, "
                   "split_parts_sent = 0, split_restore_target = NULL WHERE id = ?", (video_id,))


def _ready_split_parts(v: dict) -> list:
    """Diskda saqlangan, yuborishga tayyor qismlar (yo'q/to'liq bo'lmasa - [])."""
    total = v["split_total_parts"] or 0
    if v["split_status"] != "ready" or not total:
        return []
    if total == 1:
        return [Path(v["path"])] if v["path"] and Path(v["path"]).exists() else []
    parts = sorted((SPLIT_DIR / v["id"]).glob("part_*.mp4"))
    return parts if len(parts) == total else []


async def _send_split_video(video_id: str, start: int):
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        return
    parts = _ready_split_parts(v)
    if not parts:
        # Video hali bo'linmagan (yoki asliga qaytarilgan) - avval bo'linadi.
        start = 1
        parts = await _prepare_split_video(video_id)
        if parts is None:
            v = db.fetchone("SELECT split_error FROM videos WHERE id = ?", (video_id,))
            _mark_telegram_error(video_id, f"Videoni bo'lib bo'lmadi: {v['split_error'] if v else ''}")
            return
    try:
        await _send_parts_to_telegram(video_id, parts, v["original_name"], start=start)
    except Exception as e:
        _mark_telegram_error(video_id, e)
        return
    _mark_telegram_sent(video_id, len(parts))


@app.post("/api/videos/{video_id}/send-to-bot")
async def send_to_bot_endpoint(video_id: str, request: Request, provider: str = None, subtitled: bool = False):
    """provider berilmasa (eski, oddiy holat) - asosiy yakuniy video yuboriladi,
    xuddi avvalgidek. provider='aisha'/'openai' berilsa - shu QO'SHIMCHA track
    videosi yuboriladi (video 'completed' bo'lgach ikkinchi provayder bilan
    yaratilgan bo'lsa). subtitled=true berilsa - subtitr "kuydirilgan" (hardsub)
    variant yuboriladi (asosiy yoki tanlangan provayder treki uchun, qaysi
    tayyor bo'lsa)."""
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if provider != "learning" and v["status"] != "completed":
        raise HTTPException(400, "Avval yakuniy video tayyor bo'lishi kerak.")

    title_suffix = ""
    if provider == "learning":
        track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            raise HTTPException(400, "Ruscha o'rganish videosi hali tayyor emas.")
        use_export = (track["export_status"] == "ready" and track["export_video_path"]
                      and Path(track["export_video_path"]).exists())
        if any(b.get("words") for b in worker.learning_blocks(track)) and not use_export:
            raise HTTPException(409, "So'zlar videoga yozilmoqda. Botga yuborishdan oldin tayyor bo'lishini kuting.")
        if subtitled:
            if track["subtitled_video_status"] != "ready" or not track["subtitled_video_path"]:
                raise HTTPException(400, "Ruscha o'rganish subtitrli videosi hali tayyor emas.")
            video_path = track["subtitled_video_path"]
            title_suffix = " (ruscha o'rganish) [subtitrli]"
        else:
            video_path = track["export_video_path"] if use_export else track["final_video_path"]
            title_suffix = " (ruscha o'rganish)"
    elif provider:
        track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?",
                             (video_id, provider))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            raise HTTPException(400, "Bu provayder uchun yakuniy video hali tayyor emas.")
        video_path = track["final_video_path"]
        title_suffix = f" ({provider})"
        if subtitled:
            if track["subtitled_video_status"] != "ready" or not track["subtitled_video_path"]:
                raise HTTPException(400, "Bu provayder uchun subtitrli video hali tayyor emas.")
            video_path = track["subtitled_video_path"]
            title_suffix += " [subtitrli]"
    else:
        video_path = v["final_video_path"]
        if subtitled:
            if v["subtitled_video_status"] != "ready" or not v["subtitled_video_path"]:
                raise HTTPException(400, "Subtitrli video hali tayyor emas.")
            video_path = v["subtitled_video_path"]
            title_suffix = " [subtitrli]"
    if not video_path:
        raise HTTPException(400, "Avval yakuniy video tayyor bo'lishi kerak.")

    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise HTTPException(400, "Botga ulanish sozlanmagan (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                                  "environment variable'lari kiritilmagan).")
    db.execute("UPDATE videos SET telegram_send_status = 'sending', telegram_send_error = NULL WHERE id = ?",
               (video_id,))
    asyncio.create_task(_send_video_file_to_telegram(video_id, video_path, v["original_name"] + title_suffix))

    return {"ok": True}


def _get_split_video(video_id: str) -> dict:
    v = db.fetchone("SELECT * FROM videos WHERE id = ? AND kind = 'split_only'", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["split_status"] == "splitting":
        raise HTTPException(409, "Video hali qismlarga bo'linmoqda - tugashini kuting.")
    if v["split_status"] == "restoring":
        raise HTTPException(409, "Video hozir asliga qaytarilmoqda - tugashini kuting.")
    if v["telegram_send_status"] == "sending":
        raise HTTPException(409, "Video hozir botga yuborilmoqda - tugashini kuting.")
    if v["bot_upload_status"] == "uploading":
        raise HTTPException(409, "Video hozir Bot bo'limiga yuklanmoqda - tugashini kuting.")
    if not (v["path"] and Path(v["path"]).exists()) and not _ready_split_parts(v):
        raise HTTPException(400, "Video fayli serverda topilmadi.")
    return v


@app.post("/api/split-videos/{video_id}/split")
async def split_video_endpoint(video_id: str):
    v = _get_split_video(video_id)
    if not Path(v["path"]).exists():
        raise HTTPException(400, "Video allaqachon qismlarga bo'lingan.")
    db.execute("UPDATE videos SET split_status = 'splitting', split_error = NULL WHERE id = ?", (video_id,))
    asyncio.create_task(_prepare_split_video(video_id))
    return {"ok": True}


@app.post("/api/split-videos/{video_id}/send")
async def send_split_video_endpoint(video_id: str):
    v = _get_split_video(video_id)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        raise HTTPException(400, "Telegram bot sozlanmagan (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID).")
    # Oldingi yuborish o'rtada uzilgan bo'lsa - yuborilgan qismlardan keyin davom etadi.
    sent, total = v["split_parts_sent"] or 0, v["split_total_parts"] or 0
    start = sent + 1 if v["telegram_send_status"] == "error" and 0 < sent < total else 1
    db.execute("UPDATE videos SET telegram_send_status = 'sending', telegram_send_error = NULL WHERE id = ?",
               (video_id,))
    asyncio.create_task(_send_split_video(video_id, start))
    return {"ok": True}


@app.post("/api/split-videos/{video_id}/restore")
async def restore_split_video_endpoint(video_id: str):
    """Qismlarni qayta bitta asl videoga yig'adi."""
    v = _get_split_video(video_id)
    if Path(v["path"]).exists():
        raise HTTPException(400, "Asl video serverda bor - qaytarish shart emas.")
    db.execute("UPDATE videos SET split_status = 'restoring', split_error = NULL, split_restore_target = NULL "
               "WHERE id = ?", (video_id,))
    asyncio.create_task(_restore_split_video(video_id))
    return {"ok": True}


@app.post("/api/split-videos/{video_id}/to-library")
async def split_video_to_library_endpoint(video_id: str):
    """Videoni Kutubxonaga (tarjima quvuriga) o'tkazadi; asli o'chirilgan bo'lsa
    avval qismlardan tiklanadi."""
    _get_split_video(video_id)
    db.execute("UPDATE videos SET split_status = 'restoring', split_error = NULL, split_restore_target = 'library' "
               "WHERE id = ?", (video_id,))
    asyncio.create_task(_restore_split_video(video_id))
    return {"ok": True}


# ---------------------------------------------------------------------------
#                          RESUMABLE UPLOAD
# ---------------------------------------------------------------------------

@app.post("/api/videos/upload/init")
async def upload_init(original_name: str = Form(...), total_size: int = Form(...), kind: str = Form("pipeline"),
                       file_kind: str = Form("video")):
    user = auth.current_user()
    owner_id = user["id"]
    if user["role"] != "superadmin" and kind != "pipeline":
        raise HTTPException(403, "Oddiy foydalanuvchi faqat Kutubxonaga video yuklay oladi.")
    if kind not in ("pipeline", "split_only", "cloud"):
        raise HTTPException(400, "Noto'g'ri kind qiymati.")
    if file_kind not in ("video", "image", "file", "zip"):
        raise HTTPException(400, "Noto'g'ri file_kind qiymati.")
    if total_size > MAX_UPLOAD_SIZE:
        raise HTTPException(400, f"Fayl juda katta (limit: {MAX_UPLOAD_SIZE // (1024**3)} GB).")
    if not has_space_for(total_size) or not auth.has_user_space(total_size, user):
        raise HTTPException(400, "Sizga ajratilgan saqlash joyi yetarli emas.")

    name = safe_name(original_name)
    if kind != "cloud":
        existing_video = db.fetchone(
            "SELECT * FROM videos WHERE owner_id = ? AND original_name = ? AND file_size = ? AND kind = ? "
            "AND status != 'error' LIMIT 1", (owner_id, name, total_size, kind))
        if existing_video and existing_video["status"] != "uploading":
            return {"duplicate_of": video_public(existing_video) if kind == "pipeline" else split_video_public(existing_video)}

    existing_upload = db.fetchone(
        "SELECT * FROM uploads WHERE owner_id = ? AND original_name = ? AND total_size = ? AND kind = ? "
        "AND status = 'uploading' LIMIT 1", (owner_id, name, total_size, kind))
    if existing_upload:
        return {"upload_id": existing_upload["id"], "received_size": existing_upload["received_size"],
                "received_ranges": _upload_ranges(existing_upload), "resumed": True}

    upload_id = db.new_id()
    tmp_path = UPLOADS_DIR / f"{upload_id}.part"
    tmp_path.touch()
    db.execute(
        """INSERT INTO uploads (id, original_name, total_size, received_size, tmp_path, status, kind, file_kind,
           created_at, updated_at, owner_id) VALUES (?, ?, ?, 0, ?, 'uploading', ?, ?, ?, ?, ?)""",
        (upload_id, name, total_size, str(tmp_path), kind, file_kind, db.now(), db.now(), owner_id),
    )
    return {"upload_id": upload_id, "received_size": 0, "resumed": False}


def _upload_ranges(u: dict) -> list:
    """Serverga yetib kelgan bayt oraliqlari [[offset, size], ...]. Bo'laklar
    parallel (tartibsiz) yuboriladi, shuning uchun bitta received_size yetmaydi."""
    rows = db.fetchall("SELECT chunk_offset, chunk_size FROM upload_chunks WHERE upload_id = ? "
                       "ORDER BY chunk_offset", (u["id"],))
    if not rows and u["received_size"]:
        # Ketma-ket yuklashning eski yozuvi: boshidan received_size'gacha tayyor.
        db.execute("INSERT OR IGNORE INTO upload_chunks (upload_id, chunk_offset, chunk_size) VALUES (?, 0, ?)",
                   (u["id"], u["received_size"]))
        return [[0, u["received_size"]]]
    return [[r["chunk_offset"], r["chunk_size"]] for r in rows]


def _upload_complete_coverage(u: dict) -> bool:
    end = 0
    for start, size in sorted(_upload_ranges(u)):
        if start > end:
            return False
        end = max(end, start + size)
    return end >= u["total_size"]


@app.get("/api/videos/upload/{upload_id}")
async def upload_status(upload_id: str):
    u = db.fetchone("SELECT * FROM uploads WHERE id = ? AND owner_id = ?", (upload_id, auth.current_user_id()))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    return {"id": u["id"], "received_size": u["received_size"], "total_size": u["total_size"], "status": u["status"]}


@app.post("/api/videos/upload/{upload_id}/chunk")
async def upload_chunk(upload_id: str, offset: int = Form(...), chunk: UploadFile = File(...)):
    user = auth.current_user()
    u = db.fetchone("SELECT * FROM uploads WHERE id = ? AND owner_id = ?", (upload_id, user["id"]))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    if u["status"] != "uploading":
        raise HTTPException(400, f"Upload holati '{u['status']}'.")
    if offset < 0 or offset >= max(u["total_size"], 1):
        raise HTTPException(400, "Noto'g'ri offset.")
    _upload_ranges(u)  # eski ketma-ket yuklash bo'lsa - uning qismini ham ro'yxatga oladi

    # Bo'laklar bir vaqtda (parallel) va istalgan tartibda keladi - har biri
    # faylning o'z joyiga yoziladi.
    fd = os.open(u["tmp_path"], os.O_WRONLY | os.O_CREAT, 0o644)
    position = offset
    try:
        while True:
            part = await chunk.read(1024 * 1024)
            if not part:
                break
            if position + len(part) > u["total_size"]:
                raise HTTPException(400, "Bo'lak fayl hajmidan oshib ketdi.")
            os.pwrite(fd, part, position)
            position += len(part)
    finally:
        os.close(fd)

    if not has_space_for(0) or not auth.has_user_space(0, user):
        db.execute("UPDATE uploads SET status = 'error', updated_at = ? WHERE id = ?", (db.now(), upload_id))
        raise HTTPException(400, "Serverda joy tugadi, upload to'xtatildi.")
    db.execute("INSERT OR REPLACE INTO upload_chunks (upload_id, chunk_offset, chunk_size) VALUES (?, ?, ?)",
               (upload_id, offset, position - offset))
    received = db.fetchone("SELECT COALESCE(SUM(chunk_size), 0) n FROM upload_chunks WHERE upload_id = ?",
                           (upload_id,))["n"]
    received = min(int(received), u["total_size"])
    db.execute("UPDATE uploads SET received_size = ?, updated_at = ? WHERE id = ?", (received, db.now(), upload_id))
    return {"received_size": received, "total_size": u["total_size"]}


@app.post("/api/videos/upload/{upload_id}/complete")
async def upload_complete(upload_id: str, request: Request):
    owner_id = auth.current_user_id()
    u = db.fetchone("SELECT * FROM uploads WHERE id = ? AND owner_id = ?", (upload_id, owner_id))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    tmp_path = Path(u["tmp_path"])
    actual_size = tmp_path.stat().st_size if tmp_path.exists() else 0
    if actual_size != u["total_size"] or not _upload_complete_coverage(u):
        raise HTTPException(400, f"Fayl to'liq yuklanmagan ({u['received_size']} / {u['total_size']} bayt). "
                                  f"Qolgan qismini yuklashda davom eting.")
    db.execute("DELETE FROM upload_chunks WHERE upload_id = ?", (upload_id,))

    kind = u["kind"] or "pipeline"

    if kind == "cloud":
        cloud_id = db.new_id()
        dest_dir = CLOUD_DIR / cloud_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / u["original_name"]
        shutil.move(str(tmp_path), str(dest_path))
        db.execute(
            """INSERT INTO cloud_files (id, kind, original_name, filename, path, file_size, created_at, owner_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cloud_id, u["file_kind"] or "video", u["original_name"], dest_path.name, str(dest_path),
             u["total_size"], db.now(), owner_id),
        )
        db.execute("UPDATE uploads SET status = 'completed' WHERE id = ?", (upload_id,))
        if (u["file_kind"] or "video") == "video":
            _generate_cloud_thumbnail(cloud_id, dest_dir, dest_path)
        elif u["file_kind"] == "zip":
            await asyncio.to_thread(cloud_zip.on_zip_added, cloud_id, dest_path)
        return {"cloud_file_id": cloud_id}

    video_id = db.new_id()
    dest_dir = VIDEOS_DIR / video_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / u["original_name"]
    shutil.move(str(tmp_path), str(dest_path))

    init_status = "uploaded" if kind == "pipeline" else "completed"
    init_message = "Serverda saqlangan. Bo'laklarga avtomatik bo'linmoqda..." if kind == "pipeline" else "Yuklandi, botga yuborilmoqda..."
    db.execute(
        """INSERT INTO videos (id, original_name, filename, path, file_size, status, kind,
           created_at, updated_at, message, owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, u["original_name"], dest_path.name, str(dest_path), u["total_size"], init_status, kind,
         db.now(), db.now(), init_message, owner_id),
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

    # Video/audio serverga TO'LIQ va muvaffaqiyatli yuklangach, foydalanuvchi
    # "Bo'laklarga bo'lish"ni bosishini kutmasdan, 5 daqiqalik bo'laklarga
    # bo'lish avtomatik navbatga qo'yiladi. Transkripsiya bunga kirmaydi - u
    # hamon faqat foydalanuvchi "Transkripsiyani boshlash"ni bosganda ketadi.
    if kind == "pipeline":
        worker.enqueue_segment(video_id)

    if kind == "split_only":
        # Faqat bo'linadi - Telegram'ga foydalanuvchi "Botga jo'natish"ni bosganda yuboriladi.
        db.execute("UPDATE videos SET split_status = 'splitting' WHERE id = ?", (video_id,))
        asyncio.create_task(_prepare_split_video(video_id))

    return {"video_id": video_id}


@app.delete("/api/videos/upload/{upload_id}")
async def upload_cancel(upload_id: str):
    u = db.fetchone("SELECT * FROM uploads WHERE id = ? AND owner_id = ?", (upload_id, auth.current_user_id()))
    if not u:
        raise HTTPException(404, "Upload topilmadi.")
    Path(u["tmp_path"]).unlink(missing_ok=True)
    db.execute("UPDATE uploads SET status = 'cancelled' WHERE id = ?", (upload_id,))
    db.execute("DELETE FROM upload_chunks WHERE upload_id = ?", (upload_id,))
    return {"ok": True}


@app.get("/api/uploads")
async def list_uploads():
    return db.fetchall("SELECT id, original_name, total_size, received_size, status, created_at "
                        "FROM uploads WHERE owner_id = ? AND status IN ('uploading','error') ORDER BY created_at DESC",
                       (auth.current_user_id(),))


@app.delete("/api/uploads/cleanup")
async def cleanup_uploads():
    rows = db.fetchall("SELECT * FROM uploads WHERE owner_id = ? AND status IN ('cancelled', 'error')",
                       (auth.current_user_id(),))
    for u in rows:
        Path(u["tmp_path"]).unlink(missing_ok=True)
        db.execute("DELETE FROM uploads WHERE id = ?", (u["id"],))
    return {"removed": len(rows)}


# ---------------------------------------------------------------------------
#                          BULUT (umumiy fayl saqlash - video/rasm/hujjat)
# ---------------------------------------------------------------------------

def cloud_file_public(f: dict) -> dict:
    return {"id": f["id"], "kind": f["kind"], "original_name": f["original_name"],
            "file_size": f["file_size"], "created_at": f["created_at"],
            "has_thumbnail": bool(f["thumbnail_path"]) if "thumbnail_path" in f.keys() else False,
            "zip_entry_count": f.get("zip_entry_count"), "zip_total_size": f.get("zip_total_size"),
            "extract_status": f.get("extract_status") or "none", "extract_progress": f.get("extract_progress"),
            "extract_error": f.get("extract_error"),
            "bot_upload_status": f.get("bot_upload_status") or "none", "bot_upload_error": f.get("bot_upload_error"),
            "bot_upload_progress": f.get("bot_upload_progress") or ""}


def _generate_cloud_thumbnail(cloud_id: str, dest_dir: Path, video_path: Path):
    """Bulutga tushgan video uchun kichik rasmcha (thumbnail) yaratadi - ro'yxatda
    fayl nomi/hajmi o'rniga ko'rgazmali ko'rinish uchun. Xato bo'lsa jim o'tkazib
    yuboradi (thumbnail ixtiyoriy, asosiy yuklashni to'xtatmasligi kerak)."""
    try:
        thumb_path = dest_dir / "thumb.jpg"
        if transcription.generate_thumbnail(video_path, thumb_path):
            db.execute("UPDATE cloud_files SET thumbnail_path = ? WHERE id = ?", (str(thumb_path), cloud_id))
    except Exception:
        pass


@app.get("/api/cloud-files/{cloud_id}/thumbnail")
async def get_cloud_thumbnail(cloud_id: str):
    f = db.fetchone("SELECT thumbnail_path FROM cloud_files WHERE id = ?", (cloud_id,))
    if not f or not f["thumbnail_path"] or not Path(f["thumbnail_path"]).exists():
        raise HTTPException(404, "Thumbnail topilmadi.")
    return FileResponse(f["thumbnail_path"], media_type="image/jpeg")


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

    admin = db.fetchone("SELECT id FROM users WHERE role = 'superadmin' ORDER BY created_at LIMIT 1")
    if not admin:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(503, "Super-admin hisobi topilmadi.")
    db.execute(
        """INSERT INTO cloud_files (id, kind, original_name, filename, path, file_size, created_at, owner_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (cloud_id, "video", name, dest_path.name, str(dest_path), total, db.now(), admin["id"]),
    )
    _generate_cloud_thumbnail(cloud_id, dest_dir, dest_path)
    return {"ok": True, "cloud_file_id": cloud_id}


@app.get("/api/cloud-files")
async def list_cloud_files(kind: str = None):
    owner_id = auth.current_user_id()
    if kind:
        rows = db.fetchall("SELECT * FROM cloud_files WHERE owner_id = ? AND kind = ? ORDER BY created_at DESC",
                           (owner_id, kind))
    else:
        rows = db.fetchall("SELECT * FROM cloud_files WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,))
    return [cloud_file_public(f) for f in rows]


@app.delete("/api/cloud-files/{cloud_id}")
async def delete_cloud_file(cloud_id: str):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ?", (cloud_id,))
    if not f:
        raise HTTPException(404, "Fayl topilmadi.")
    if f.get("extract_status") == "extracting":
        raise HTTPException(409, "Bu zipdan hozir fayllar chiqarilmoqda - tugashini kuting.")
    if f.get("bot_upload_status") == "uploading":
        raise HTTPException(409, "Bu video hozir Bot bo'limiga yuklanmoqda - tugashini kuting.")
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
           created_at, updated_at, message, owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, f["original_name"], dest_path.name, str(dest_path), f["file_size"], init_status, kind,
         db.now(), db.now(), init_message, f["owner_id"]),
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
    if f.get("bot_upload_status") == "uploading":
        raise HTTPException(409, "Bu video hozir Bot bo'limiga yuklanmoqda - tugashini kuting.")
    if not has_space_for(f["file_size"]):
        raise HTTPException(400, "Serverda yetarli bo'sh joy yo'q.")
    video_id = _move_cloud_video_into_pipeline(f, "pipeline")
    return {"video_id": video_id}


@app.post("/api/cloud-files/{cloud_id}/use-in-split")
async def use_cloud_file_in_split(cloud_id: str, request: Request):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND kind = 'video'", (cloud_id,))
    if not f:
        raise HTTPException(404, "Bulutda bunday video topilmadi.")
    if f.get("bot_upload_status") == "uploading":
        raise HTTPException(409, "Bu video hozir Bot bo'limiga yuklanmoqda - tugashini kuting.")
    if not has_space_for(f["file_size"]):
        raise HTTPException(400, "Serverda yetarli bo'sh joy yo'q.")
    video_id = _move_cloud_video_into_pipeline(f, "split_only")
    db.execute("UPDATE videos SET split_status = 'splitting' WHERE id = ?", (video_id,))
    asyncio.create_task(_prepare_split_video(video_id))
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
    owner_id = auth.current_user_id()
    videos = db.fetchall(
        "SELECT id, original_name, status, blocked_reason, progress, message, chunk_count, created_at FROM videos "
        "WHERE owner_id = ? AND status NOT IN ('uploading') ORDER BY created_at DESC", (owner_id,))
    video_jobs = [{"id": v["id"], "type": "video", "title": v["original_name"], "status": v["status"],
                    "blocked_reason": v["blocked_reason"], "phase": PHASE_LABELS.get(v["status"], v["status"]),
                    "progress": v["progress"], "message": v["message"], "total": v["chunk_count"]} for v in videos]
    tts_jobs = db.fetchall(
        "SELECT id, title, status, total_segments, completed_segments, error FROM tts_jobs "
        "WHERE owner_id = ? AND video_id IS NULL ORDER BY created_at DESC", (owner_id,))
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
async def job_retry_chunk(video_id: str, chunk_id: str, language: str = Form(None)):
    """`language` berilsa (None emas - bo'sh satr ham "ataylab avtomatik" degani),
    shu bo'lak uchun til ATAYLAB shu qiymatga o'rnatiladi va Whisper so'roviga
    majburiy yuboriladi - avvalgi (noto'g'ri) til bilan qayta-qayta xato natija
    olish muammosi shu bilan tuzatiladi."""
    _ensure_video(video_id)
    if language is not None:
        _validate_transcribe_language(language)
    worker.retry_chunk(video_id, chunk_id, language)
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
async def download_final_video(video_id: str, request: Request, provider: str = None):
    v = _ensure_video(video_id)
    video_path = v["final_video_path"]
    if provider == "learning":
        track = db.fetchone(
            "SELECT final_video_path, final_video_status, export_video_path, export_status, words_json "
            "FROM learning_tracks WHERE video_id = ?", (video_id,))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            raise HTTPException(404, "Ruscha o'rganish videosi topilmadi.")
        export_ready = (track["export_status"] == "ready" and track["export_video_path"]
                        and Path(track["export_video_path"]).exists())
        if any(b.get("words") for b in worker.learning_blocks(track)) and not export_ready:
            raise HTTPException(409, "So'zlar videoga yozilmoqda. Tayyor bo'lgach qayta urinib ko'ring.")
        video_path = track["export_video_path"] if export_ready else track["final_video_path"]
    elif provider:
        track = db.fetchone(
            "SELECT final_video_path, final_video_status FROM audio_tracks WHERE video_id = ? AND provider = ?",
            (video_id, provider))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            raise HTTPException(404, "Bu provayder uchun yakuniy video topilmadi.")
        video_path = track["final_video_path"]
    if not video_path or not Path(video_path).exists():
        raise HTTPException(404, "Yakuniy video topilmadi.")
    return range_file_response(request, Path(video_path), "video/mp4")


@app.get("/api/videos/{video_id}/original-stream")
async def stream_original_video(video_id: str, request: Request):
    v = _ensure_video(video_id)
    if not v["path"] or not Path(v["path"]).exists():
        raise HTTPException(404, "Original video topilmadi.")
    return range_file_response(request, Path(v["path"]), "video/mp4")


def _result_by_kind(video_id: str, kind: str):
    return db.fetchone("SELECT * FROM results WHERE video_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 1",
                        (video_id, kind))


def _parse_points(raw) -> list:
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def _timeline_points_for(video_id: str, timeline: str) -> list:
    """timeline: "source" - original vaqt; "final" - asosiy o'zbekcha video;
    "final:<provider>" - shu provayderning qo'shimcha treki videosi."""
    if not timeline or timeline == "source":
        return []
    if timeline == "final":
        v = db.fetchone("SELECT freeze_points FROM videos WHERE id = ?", (video_id,))
        return _parse_points(v["freeze_points"]) if v else []
    if timeline.startswith("final:"):
        t = db.fetchone("SELECT freeze_points FROM audio_tracks WHERE video_id = ? AND provider = ?",
                        (video_id, timeline.split(":", 1)[1]))
        return _parse_points(t["freeze_points"]) if t else []
    raise HTTPException(400, "timeline: source, final yoki final:<provider> bo'lishi kerak.")


_VTT_TIME_RE = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")


def _vtt_on_timeline(path: Path, points: list) -> str:
    """Manba vaqtidagi VTT ni tanlangan videoning vaqt chizig'iga o'tkazadi
    (transcription.source_time_to_final_time orqali - render bilan bir xil)."""
    text = path.read_text(encoding="utf-8")
    active = transcription.active_timeline_points(points)
    if not active:
        return text

    def shift(m):
        sec = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
        return transcription.fmt_vtt_time(transcription.source_time_to_final_time(sec, active))

    return "\n".join(_VTT_TIME_RE.sub(shift, line) if "-->" in line else line for line in text.split("\n"))


@app.get("/api/videos/{video_id}/subtitles/original.vtt")
async def subtitles_original_vtt(video_id: str, timeline: str = "source"):
    r = _result_by_kind(video_id, "vtt_original")
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "Original subtitr topilmadi.")
    points = _timeline_points_for(video_id, timeline)
    if not points:
        return FileResponse(r["path"], media_type="text/vtt")
    return Response(_vtt_on_timeline(Path(r["path"]), points), media_type="text/vtt")


@app.get("/api/videos/{video_id}/subtitles/uz.vtt")
async def subtitles_uz_vtt(video_id: str, provider: str = None, timeline: str = None):
    # timeline berilsa (pleyer shuni ishlatadi): manba vaqtidagi vtt_uz tanlangan
    # videoning vaqt chizig'iga o'tkaziladi - audio "Original" bo'lsa "source".
    if timeline:
        r = _result_by_kind(video_id, "vtt_uz")
        if r and Path(r["path"]).exists():
            return Response(_vtt_on_timeline(Path(r["path"]), _timeline_points_for(video_id, timeline)),
                            media_type="text/vtt")
    # "uz" video treki - freeze bo'lsa - kadr kutib turishi bilan cho'zilgan yakuniy
    # videodir, shuning uchun mos keladigan subtitr ham freeze bilan moslashtirilgan
    # variant (vtt_uz_final) bo'lishi kerak, agar u mavjud bo'lsa. "Original" trek
    # hech qachon freeze bilan o'zgartirilmaydi, shu sabab uning subtitri doim manba
    # (vtt_original) bo'lib qoladi - subtitles_original_vtt bunga tegilmagan.
    # provider berilsa - qo'shimcha (video 'completed' bo'lgach ikkinchi provayder
    # bilan yaratilgan) trekning O'ZINING freeze bilan moslashtirilgan varianti
    # ishlatiladi (uning freeze nuqtalari asosiy trekdan farq qilishi mumkin).
    if provider:
        r = _result_by_kind(video_id, f"vtt_uz_final_{provider}") or _result_by_kind(video_id, "vtt_uz")
    else:
        r = _result_by_kind(video_id, "vtt_uz_final") or _result_by_kind(video_id, "vtt_uz")
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
                             aisha_key.strip(), stretch_to_fit, owner_id=auth.current_user_id())
    return {"id": job_id, "segments": len(segments)}


@app.get("/api/tts/jobs")
async def list_tts_jobs():
    return db.fetchall("SELECT id, title, provider, status, total_segments, completed_segments, "
                        "error, created_at FROM tts_jobs WHERE owner_id = ? ORDER BY created_at DESC",
                       (auth.current_user_id(),))


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

    # amount_usd (dollar) va amount_som (Aisha, so'm) ikki xil valyuta -
    # ARALASHTIRILMAYDI, alohida-alohida yig'indi qilinadi.
    owner_id = auth.current_user_id()
    def total_since(since):
        r = db.fetchone(
            "SELECT COALESCE(SUM(amount_usd),0) usd, COALESCE(SUM(amount_som),0) som "
            "FROM costs WHERE owner_id = ? AND created_at >= ?", (owner_id, since))
        return {"usd": round(r["usd"], 4), "som": round(r["som"], 2)}

    # "tts" - eski (migratsiyadan oldingi) OpenAI TTS yozuvlari, "tts_openai" bilan
    # birga hisoblanadi (moslik uchun; Aisha'ning eski "tts" yozuvlari doim $0 edi,
    # shuning uchun bu yerga qo'shilishi hech narsani buzmaydi).
    per_video = db.fetchall(
        """SELECT v.id, v.original_name, COALESCE(SUM(c.amount_usd),0) as total,
           COALESCE(SUM(c.amount_som),0) as total_som,
           SUM(CASE WHEN c.kind='transcription' THEN c.amount_usd ELSE 0 END) as transcription,
           SUM(CASE WHEN c.kind='translation' THEN c.amount_usd ELSE 0 END) as translation,
           SUM(CASE WHEN c.kind IN ('tts_openai','tts') THEN c.amount_usd ELSE 0 END) as tts_openai,
           SUM(CASE WHEN c.kind='tts_aisha' THEN c.amount_som ELSE 0 END) as tts_aisha_som,
           SUM(CASE WHEN c.kind='stt_elevenlabs' THEN c.amount_usd ELSE 0 END) as stt_elevenlabs,
           SUM(CASE WHEN c.kind='stt_diarize' THEN c.amount_usd ELSE 0 END) as stt_diarize
           FROM videos v LEFT JOIN costs c ON c.video_id = v.id
           WHERE v.owner_id = ? GROUP BY v.id ORDER BY v.created_at DESC""", (owner_id,))
    # O'chirilgan videolarning xarajatlari ham hisobotda qoladi.
    deleted = db.fetchall(
        """SELECT c.video_id as id, MAX(c.video_name) as original_name, COALESCE(SUM(c.amount_usd),0) as total,
           COALESCE(SUM(c.amount_som),0) as total_som,
           SUM(CASE WHEN c.kind='transcription' THEN c.amount_usd ELSE 0 END) as transcription,
           SUM(CASE WHEN c.kind='translation' THEN c.amount_usd ELSE 0 END) as translation,
           SUM(CASE WHEN c.kind IN ('tts_openai','tts') THEN c.amount_usd ELSE 0 END) as tts_openai,
           SUM(CASE WHEN c.kind='tts_aisha' THEN c.amount_som ELSE 0 END) as tts_aisha_som,
           SUM(CASE WHEN c.kind='stt_elevenlabs' THEN c.amount_usd ELSE 0 END) as stt_elevenlabs,
           SUM(CASE WHEN c.kind='stt_diarize' THEN c.amount_usd ELSE 0 END) as stt_diarize
           FROM costs c WHERE c.owner_id = ? AND c.video_id IS NOT NULL
           AND c.video_id NOT IN (SELECT id FROM videos)
           GROUP BY c.video_id ORDER BY MAX(c.created_at) DESC""", (owner_id,))
    for r in deleted:
        r["original_name"] = f"{r['original_name'] or 'Nomi saqlanmagan video'} (o'chirilgan)"
        r["deleted"] = True
    return {
        "today": total_since(today_start),
        "week": total_since(week_start),
        "month": total_since(month_start),
        "all_time": total_since("0000-01-01T00:00:00"),
        "per_video": [dict(r) for r in per_video] + [dict(r) for r in deleted],
    }


@app.get("/api/videos/{video_id}/costs")
async def get_video_costs(video_id: str):
    return db.fetchall("SELECT kind, amount_usd, amount_som, detail, created_at FROM costs "
                        "WHERE video_id = ? ORDER BY created_at ASC", (video_id,))


# ---------------------------------------------------------------------------
#                          SOZLAMALAR (Aysha va OpenAI standart qiymatlari)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "aisha_default_voice": "Gulnoza", "aisha_default_mood": "Neutral", "aisha_default_speed": "1.0",
    "openai_default_voice": "alloy", "openai_default_instructions": "",
    "default_language": "", "default_stretch_to_fit": "true",
    # Matn olish: qo'shimcha uydirma iboralar (har qatorda bittadan).
    "hallucination_phrases": "",
    "translation_instruction": "", "translation_context": "",
}


@app.get("/api/settings")
async def get_settings():
    owner_id = auth.current_user_id()
    out = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        v = db.get_user_setting(owner_id, key)
        if v is not None:
            out[key] = v
    return out


@app.post("/api/settings")
async def update_settings(payload: dict):
    owner_id = auth.current_user_id()
    for key, value in payload.items():
        if key in DEFAULT_SETTINGS:
            db.set_user_setting(owner_id, key, str(value))
    return {"ok": True}


# ---------------------------------------------------------------------------
#            TARJIMON XOTIRASI (Claude bilan kichik chat + qoidalar)
# ---------------------------------------------------------------------------

@app.get("/api/translation-memory/chat")
async def get_memory_chat():
    return db.fetchall("SELECT id, role, content, created_at FROM translation_memory_chat "
                        "WHERE owner_id = ? ORDER BY created_at ASC", (auth.current_user_id(),))


@app.post("/api/translation-memory/chat")
async def send_memory_chat(message: str = Form(...)):
    owner_id = auth.current_user_id()
    if not keys_manager.has_any_active_key(provider="claude"):
        raise HTTPException(400, "Ishlaydigan Claude API kalit topilmadi. Avval API kalit qo'shing.")
    kid, raw = keys_manager.get_next_active_key(provider="claude")

    user_msg_id = db.new_id()
    db.execute("INSERT INTO translation_memory_chat (id, role, content, created_at, owner_id) "
               "VALUES (?, 'user', ?, ?, ?)", (user_msg_id, message, db.now(), owner_id))

    history = db.fetchall("SELECT role, content FROM translation_memory_chat WHERE owner_id = ? "
                          "ORDER BY created_at ASC LIMIT 40", (owner_id,))
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
    db.execute("INSERT INTO translation_memory_chat (id, role, content, created_at, owner_id) "
               "VALUES (?, 'assistant', ?, ?, ?)", (assistant_msg_id, reply, db.now(), owner_id))
    return {"id": assistant_msg_id, "role": "assistant", "content": reply}


@app.delete("/api/translation-memory/chat")
async def clear_memory_chat():
    db.execute("DELETE FROM translation_memory_chat WHERE owner_id = ?", (auth.current_user_id(),))
    return {"ok": True}


@app.get("/api/translation-memory/notes")
async def list_memory_notes():
    return db.fetchall("SELECT id, content, created_at FROM translation_memory_notes WHERE owner_id = ? "
                       "ORDER BY created_at DESC", (auth.current_user_id(),))


@app.post("/api/translation-memory/notes")
async def add_memory_note(content: str = Form(...), source_message_id: str = Form("")):
    note_id = db.new_id()
    db.execute("INSERT INTO translation_memory_notes (id, content, source_message_id, created_at, owner_id) "
               "VALUES (?, ?, ?, ?, ?)", (note_id, content, source_message_id or None, db.now(), auth.current_user_id()))
    return {"ok": True, "id": note_id}


@app.delete("/api/translation-memory/notes/{note_id}")
async def delete_memory_note(note_id: str):
    db.execute("DELETE FROM translation_memory_notes WHERE id = ? AND owner_id = ?",
               (note_id, auth.current_user_id()))
    return {"ok": True}
