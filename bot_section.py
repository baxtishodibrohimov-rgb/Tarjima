"""Saytdagi "Bot" bo'limi - Telegram botdagi 🎬 Video Baza bilan BITTA ma'lumot.

Papka va videolar idea_folders / idea_items jadvallarida (root_type='video_base')
saqlanadi - bot ham, sayt ham shu jadvallarni o'qib-yozadi, shuning uchun biri
o'zgarsa ikkinchisida darhol ko'rinadi. Videoning o'zi Telegram'da saqlanadi
(bot egasining chatiga yuboriladi, file_id yoziladi), serverda faqat havolasi
qoladi; saytda ko'rish uchun mahalliy Bot API server orqali qaytarib olinadi.
"""
import asyncio
import os
import shutil
import traceback
from pathlib import Path

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

import database as db
import ideaflow_bot
import transcription
from storage import IDEA_BOT_TOKEN, LOCAL_BOT_API_URL, SPLIT_DIR

router = APIRouter(prefix="/api/bot")

ROOT = "video_base"
# Mahalliy Bot API server getFile vaqtida katta faylni Telegram'dan yuklab oladi.
GET_FILE_TIMEOUT = 2 * 60 * 60
UPLOAD_TIMEOUT = 30 * 60
VARIANTS = {
    "final": ("final_video_path", ""),
    "subtitled": ("subtitled_video_path", " [subtitrli]"),
    "original": ("path", " (asl)"),
}


def _bot_url(method: str) -> str:
    return f"{LOCAL_BOT_API_URL.rstrip('/')}/bot{IDEA_BOT_TOKEN}/{method}"


def _owner() -> dict:
    owner = ideaflow_bot.owner_profile()
    if not owner:
        raise HTTPException(409, "Bot hali ulanmagan: avval Telegram'da botga /start yozing.")
    return owner


def _folder(owner: dict, folder_id: str) -> dict:
    f = db.fetchone("SELECT * FROM idea_folders WHERE id = ? AND user_id = ? AND root_type = ?",
                    (folder_id, owner["id"], ROOT))
    if not f:
        raise HTTPException(404, "Papka topilmadi.")
    return f


def _item(owner: dict, item_id: str) -> dict:
    it = db.fetchone("SELECT * FROM idea_items WHERE id = ? AND user_id = ? AND root_type = ?",
                     (item_id, owner["id"], ROOT))
    if not it:
        raise HTTPException(404, "Video topilmadi.")
    return it


def _parts(item_id: str) -> list:
    return db.fetchall("SELECT telegram_file_id, file_kind, file_name, file_size FROM idea_attachments "
                       "WHERE related_type = 'item' AND related_id = ? AND telegram_file_id IS NOT NULL "
                       "ORDER BY rowid", (item_id,))


def _descendants(owner_id: str, folder_id: str) -> set:
    rows = db.fetchall("SELECT id, parent_folder_id FROM idea_folders WHERE user_id = ? AND root_type = ?",
                       (owner_id, ROOT))
    found = {folder_id}
    changed = True
    while changed:
        changed = False
        for r in rows:
            if r["id"] not in found and r["parent_folder_id"] in found:
                found.add(r["id"])
                changed = True
    return found


# ------------------------------------------------------------------ ko'rish

@router.get("/tree")
def bot_tree():
    owner = ideaflow_bot.owner_profile()
    uploads = db.fetchall(
        "SELECT id, original_name, bot_upload_status, bot_upload_error, bot_upload_progress, "
        "bot_upload_folder_id, bot_upload_title FROM videos WHERE bot_upload_status IN ('uploading', 'error') "
        "ORDER BY updated_at DESC")
    if not owner:
        return {"linked": False, "folders": [], "items": [], "uploads": uploads, "bot_username": ""}
    folders = db.fetchall("SELECT id, name, parent_folder_id FROM idea_folders WHERE user_id = ? AND root_type = ? "
                          "ORDER BY sort_order, created_at", (owner["id"], ROOT))
    items = db.fetchall("SELECT id, title, folder_id, type, url, created_at FROM idea_items "
                        "WHERE user_id = ? AND root_type = ? ORDER BY created_at DESC", (owner["id"], ROOT))
    parts = {}
    for a in db.fetchall(
            "SELECT a.related_id, a.file_kind, a.file_name, a.file_size FROM idea_attachments a "
            "JOIN idea_items i ON i.id = a.related_id WHERE i.user_id = ? AND i.root_type = ? "
            "AND a.related_type = 'item' AND a.telegram_file_id IS NOT NULL ORDER BY a.rowid", (owner["id"], ROOT)):
        parts.setdefault(a["related_id"], []).append(
            {"file_kind": a["file_kind"], "file_name": a["file_name"], "file_size": a["file_size"]})
    for it in items:
        it["parts"] = parts.get(it["id"], [])
    return {"linked": True, "folders": folders, "items": items, "uploads": uploads,
            "bot_username": db.get_setting(ideaflow_bot.BOT_USERNAME_KEY, "") or ""}


# ------------------------------------------------------------------ papkalar

@router.post("/folders")
def create_folder(name: str = Form(...), parent_id: str = Form("")):
    owner = _owner()
    name = name.strip()[:80]
    if not name:
        raise HTTPException(400, "Papka nomini kiriting.")
    if parent_id:
        _folder(owner, parent_id)
    now = db.now()
    folder_id = db.new_id()
    db.execute("INSERT INTO idea_folders (id, user_id, parent_folder_id, root_type, name, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)", (folder_id, owner["id"], parent_id or None, ROOT, name, now, now))
    return {"id": folder_id}


@router.post("/folders/{folder_id}/rename")
def rename_folder(folder_id: str, name: str = Form(...)):
    owner = _owner()
    _folder(owner, folder_id)
    name = name.strip()[:80]
    if not name:
        raise HTTPException(400, "Papka nomini kiriting.")
    db.execute("UPDATE idea_folders SET name = ?, updated_at = ? WHERE id = ?", (name, db.now(), folder_id))
    return {"ok": True}


@router.post("/folders/{folder_id}/move")
def move_folder(folder_id: str, parent_id: str = Form("")):
    owner = _owner()
    _folder(owner, folder_id)
    if parent_id:
        _folder(owner, parent_id)
        if parent_id in _descendants(owner["id"], folder_id):
            raise HTTPException(400, "Papkani o'zining ichiga ko'chirib bo'lmaydi.")
    db.execute("UPDATE idea_folders SET parent_folder_id = ?, updated_at = ? WHERE id = ?",
               (parent_id or None, db.now(), folder_id))
    return {"ok": True}


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: str):
    owner = _owner()
    _folder(owner, folder_id)
    if db.fetchone("SELECT 1 FROM idea_folders WHERE parent_folder_id = ? LIMIT 1", (folder_id,)):
        raise HTTPException(409, "Avval ichki papkalarni o'chiring.")
    # Botdagi kabi: ichidagi videolar o'chmaydi, asosiy bo'limga chiqadi.
    db.execute("UPDATE idea_items SET folder_id = NULL WHERE folder_id = ? AND user_id = ?", (folder_id, owner["id"]))
    db.execute("DELETE FROM idea_folders WHERE id = ?", (folder_id,))
    return {"ok": True}


# ------------------------------------------------------------------ videolar

@router.post("/items/{item_id}/move")
def move_item(item_id: str, folder_id: str = Form("")):
    owner = _owner()
    _item(owner, item_id)
    if folder_id:
        _folder(owner, folder_id)
    db.execute("UPDATE idea_items SET folder_id = ?, updated_at = ? WHERE id = ?",
               (folder_id or None, db.now(), item_id))
    return {"ok": True}


@router.post("/items/{item_id}/rename")
def rename_item(item_id: str, title: str = Form(...)):
    owner = _owner()
    _item(owner, item_id)
    title = title.strip()[:200]
    if not title:
        raise HTTPException(400, "Nomini kiriting.")
    db.execute("UPDATE idea_items SET title = ?, updated_at = ? WHERE id = ?", (title, db.now(), item_id))
    return {"ok": True}


@router.delete("/items/{item_id}")
def delete_item(item_id: str):
    owner = _owner()
    _item(owner, item_id)
    db.execute("DELETE FROM idea_attachments WHERE related_type = 'item' AND related_id = ?", (item_id,))
    db.execute("DELETE FROM idea_items WHERE id = ?", (item_id,))
    return {"ok": True}


@router.get("/items/{item_id}/stream")
async def stream_item(item_id: str, request: Request, part: int = 1):
    """Videoni Telegram'dan (mahalliy Bot API server orqali) qaytarib olib ko'rsatadi."""
    owner = _owner()
    _item(owner, item_id)
    parts = _parts(item_id)
    if not 1 <= part <= len(parts):
        raise HTTPException(404, "Video qismi topilmadi.")
    att = parts[part - 1]
    media_type = "video/mp4" if att["file_kind"] == "video" else "application/octet-stream"

    async with httpx.AsyncClient(timeout=GET_FILE_TIMEOUT) as client:
        resp = await client.post(_bot_url("getFile"), data={"file_id": att["telegram_file_id"]})
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise HTTPException(502, f"Telegram'dan videoni olib bo'lmadi: {data.get('description') or resp.text[:200]}")
    file_path = data["result"]["file_path"]

    # Mahalliy (--local) Bot API server faylni shu serverga yuklab, mutlaq yo'lini qaytaradi.
    source = Path(file_path)
    if source.is_absolute() and source.is_file() and os.access(source, os.R_OK):
        import app
        return app.range_file_response(request, source, media_type)

    client = httpx.AsyncClient(timeout=None)
    headers = {"Range": request.headers["range"]} if "range" in request.headers else {}
    upstream = await client.send(
        client.build_request("GET", f"{LOCAL_BOT_API_URL.rstrip('/')}/file/bot{IDEA_BOT_TOKEN}/{file_path}",
                             headers=headers), stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, f"Telegram'dan videoni olib bo'lmadi (HTTP {upstream.status_code}).")

    async def close():
        await upstream.aclose()
        await client.aclose()

    passthrough = {k: upstream.headers[k] for k in ("content-length", "content-range", "accept-ranges")
                   if k in upstream.headers}
    return StreamingResponse(upstream.aiter_bytes(), status_code=upstream.status_code, headers=passthrough,
                             media_type=media_type, background=BackgroundTask(close))


# ------------------------------------------------------------------ Kutubxonadan yuklash

@router.post("/upload")
async def upload_from_library(video_id: str = Form(...), variant: str = Form("final"), folder_id: str = Form(""),
                              remove_from_library: bool = Form(True)):
    owner = _owner()
    if not IDEA_BOT_TOKEN:
        raise HTTPException(400, "Bot sozlanmagan (IDEA_BOT_TOKEN).")
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not v:
        raise HTTPException(404, "Video topilmadi.")
    if v["bot_upload_status"] == "uploading":
        raise HTTPException(409, "Bu video hozir botga yuklanmoqda.")
    if variant not in VARIANTS:
        raise HTTPException(400, "Noto'g'ri variant.")
    column, suffix = VARIANTS[variant]
    path = v[column]
    if not path or not Path(path).exists():
        raise HTTPException(400, "Bu variant uchun video fayli serverda yo'q.")
    if folder_id:
        _folder(owner, folder_id)

    title = (Path(v["original_name"] or "video").stem + suffix)[:200]
    db.execute("UPDATE videos SET bot_upload_status = 'uploading', bot_upload_error = NULL, bot_upload_progress = '', "
               "bot_upload_folder_id = ?, bot_upload_title = ? WHERE id = ?", (folder_id or None, title, video_id))
    asyncio.create_task(_upload_job(video_id, path, title, folder_id or None, remove_from_library,
                                    owner["id"], owner["telegram_chat_id"]))
    return {"ok": True}


async def _upload_job(video_id, path, title, folder_id, remove_from_library, owner_id, chat_id):
    import app
    split_dir = SPLIT_DIR / f"bot_{video_id}"
    try:
        async with app._SPLIT_LOCK:
            shutil.rmtree(split_dir, ignore_errors=True)
            parts = await asyncio.get_event_loop().run_in_executor(
                None, transcription.split_video_by_size, Path(path), split_dir)
        total = len(parts)
        location = "🎬 Video Baza / " + ideaflow_bot.folder_path(owner_id, folder_id)
        uploaded = []
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            for i, part in enumerate(parts, start=1):
                db.execute("UPDATE videos SET bot_upload_progress = ? WHERE id = ?", (f"{i - 1}/{total}", video_id))
                label = f"{title} — {i}/{total}-qism" if total > 1 else title
                filename = f"{title}_{i:02d}.mp4" if total > 1 else f"{title}.mp4"
                with open(part, "rb") as f:
                    resp = await client.post(_bot_url("sendVideo"), data={
                        "chat_id": chat_id, "caption": f"🎬 {label}\n📍 {location}"[:1000],
                        "supports_streaming": "true",
                    }, files={"video": (filename, f, "video/mp4")})
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                message = data.get("result") or {}
                media = message.get("video") or message.get("document")
                if not data.get("ok") or not media:
                    raise RuntimeError(f"Telegram xatosi (qism {i}/{total}): {resp.text[:300]}")
                uploaded.append((media["file_id"], "video" if message.get("video") else "file", filename,
                                 Path(part).stat().st_size))

        if folder_id and not db.fetchone("SELECT 1 FROM idea_folders WHERE id = ?", (folder_id,)):
            folder_id = None  # yuklash davomida papka botda o'chirilgan bo'lsa
        now = db.now()
        item_id = db.new_id()
        db.execute("INSERT INTO idea_items (id, user_id, folder_id, root_type, type, title, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, 'video', ?, ?, ?)", (item_id, owner_id, folder_id, ROOT, title, now, now))
        for file_id, kind, filename, size in uploaded:
            db.execute("INSERT INTO idea_attachments (id, user_id, related_type, related_id, file_kind, file_name, "
                       "file_size, telegram_file_id, created_at) VALUES (?, ?, 'item', ?, ?, ?, ?, ?, ?)",
                       (db.new_id(), owner_id, item_id, kind, filename, size, file_id, now))
        db.execute("UPDATE videos SET bot_upload_status = 'done', bot_upload_progress = ?, bot_item_id = ? "
                   "WHERE id = ?", (f"{total}/{total}", item_id, video_id))
        db.log_line(video_id, f"Video botdagi Video Bazaga yuklandi ({total} qism): {location}")
        if remove_from_library:
            v = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
            if v:
                app.delete_video_completely(v)
    except Exception as e:
        traceback.print_exc()
        db.execute("UPDATE videos SET bot_upload_status = 'error', bot_upload_error = ? WHERE id = ?",
                   (str(e)[:500], video_id))
        db.log_line(video_id, f"Botga yuklashda xato: {e}")
    finally:
        shutil.rmtree(split_dir, ignore_errors=True)


def recover_interrupted_uploads():
    db.execute("UPDATE videos SET bot_upload_status = 'error', bot_upload_error = ? "
               "WHERE bot_upload_status = 'uploading'",
               ("Server qayta ishga tushdi - yuklash to'xtab qoldi. Qayta urinib ko'ring.",))
