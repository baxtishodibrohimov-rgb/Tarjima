"""Saytdagi "Bot" bo'limi - Telegram botdagi 🎬 Video Baza bilan BITTA ma'lumot.

Papka va videolar idea_folders / idea_items jadvallarida (root_type='video_base')
saqlanadi - bot ham, sayt ham shu jadvallarni o'qib-yozadi, shuning uchun biri
o'zgarsa ikkinchisida darhol ko'rinadi. Videoning o'zi Telegram'da saqlanadi
(bot egasining chatiga yuboriladi, file_id yoziladi), serverda faqat havolasi
qoladi; saytda ko'rish uchun mahalliy Bot API server orqali qaytarib olinadi.
"""
import asyncio
import os
import re
import shutil
import traceback
from pathlib import Path
from urllib.parse import quote

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


PROVIDER_NAMES = {"aisha": "Aisha", "openai": "OpenAI"}
RESULT_LABELS = [  # Kutubxona natijalari (results.kind) - tartibi bilan
    ("srt", "📄 Ruscha matn (SRT)"),
    ("txt", "📄 Ruscha matn (TXT)"),
    ("srt_uz", "📄 O'zbekcha tarjima (SRT)"),
    ("srt_uz_final", "📄 O'zbekcha SRT (yakuniy videoga mos)"),
    ("srt_ru_learning_final", "📄 Ruscha o'rganish SRT"),
]


def project_files(v: dict) -> list:
    """Kutubxona loyihasining Telegram'ga yuboriladigan barcha fayllari: videolar,
    SRT/TXT matnlar va dublyaj audiolari. Pullik bosqichlar (transkripsiya, tarjima,
    TTS) natijasi shu yerda saqlanadi - videoni qayta pulsiz yig'ish mumkin bo'ladi.
    Birinchisi (asl video) botda asosiy bo'lib chiqadi, qolganlari tugma bo'ladi."""
    files, seen = [], set()

    def add(label, path, kind):
        if path and str(path) not in seen and Path(path).exists():
            seen.add(str(path))
            files.append({"label": label, "kind": kind, "path": str(path)})

    vid = v["id"]
    tracks = db.fetchall("SELECT * FROM audio_tracks WHERE video_id = ? ORDER BY rowid", (vid,))
    learning = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (vid,))
    results = {}
    for r in db.fetchall("SELECT kind, path FROM results WHERE video_id = ? ORDER BY created_at", (vid,)):
        results[r["kind"]] = r["path"]

    add("🎬 Asl video", v["path"], "video")
    add("🇺🇿 O'zbekcha video", v["final_video_path"], "video")
    add("🇺🇿 O'zbekcha video, subtitrli", v["subtitled_video_path"], "video")
    for t in tracks:
        name = PROVIDER_NAMES.get(t["provider"], t["provider"])
        add(f"🇺🇿 O'zbekcha video ({name})", t["final_video_path"], "video")
        add(f"🇺🇿 O'zbekcha video ({name}), subtitrli", t["subtitled_video_path"], "video")
    if learning:
        add("🇷🇺 Ruscha o'rganish videosi", learning["export_video_path"] or learning["final_video_path"], "video")
        add("🇷🇺 Ruscha o'rganish videosi, subtitrli", learning["subtitled_video_path"], "video")
    for kind, label in RESULT_LABELS:
        add(label, results.get(kind), "file")
        if kind == "srt_uz_final":
            for t in tracks:
                add(f"📄 O'zbekcha SRT ({PROVIDER_NAMES.get(t['provider'], t['provider'])})",
                    results.get(f"srt_uz_final_{t['provider']}"), "file")
    if learning:
        add("📄 Ruscha o'rganish SRT (manba)", learning["srt_path"], "file")
    add("🎵 O'zbekcha dublyaj audio", v.get("audio_path"), "audio")
    for t in tracks:
        add(f"🎵 Dublyaj audio ({PROVIDER_NAMES.get(t['provider'], t['provider'])})", t["audio_path"], "audio")
    if learning:
        add("🎵 Ruscha o'rganish audiosi", learning["audio_path"], "audio")
    for order, f in enumerate(files):
        f["order"] = order
    return files


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
                       "ORDER BY COALESCE(group_order, 0), rowid", (item_id,))


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
        "bot_upload_folder_id, bot_upload_title, 'library' AS source FROM videos "
        "WHERE bot_upload_status IN ('uploading', 'error') ORDER BY updated_at DESC")
    uploads += db.fetchall(
        "SELECT id, original_name, bot_upload_status, bot_upload_error, bot_upload_progress, "
        "bot_upload_folder_id, bot_upload_title, 'cloud' AS source FROM cloud_files "
        "WHERE bot_upload_status IN ('uploading', 'error') ORDER BY created_at DESC")
    if not owner:
        return {"linked": False, "folders": [], "items": [], "uploads": uploads, "bot_username": ""}
    folders = db.fetchall("SELECT id, name, parent_folder_id FROM idea_folders WHERE user_id = ? AND root_type = ? "
                          "ORDER BY sort_order, created_at", (owner["id"], ROOT))
    items = db.fetchall("SELECT id, title, folder_id, type, url, created_at FROM idea_items "
                        "WHERE user_id = ? AND root_type = ? ORDER BY created_at DESC", (owner["id"], ROOT))
    parts = {}
    for a in db.fetchall(
            "SELECT a.related_id, a.file_kind, a.file_name, a.file_size, a.group_label, "
            "COALESCE(a.group_order, 0) AS group_order FROM idea_attachments a "
            "JOIN idea_items i ON i.id = a.related_id WHERE i.user_id = ? AND i.root_type = ? "
            "AND a.related_type = 'item' AND a.telegram_file_id IS NOT NULL "
            "ORDER BY COALESCE(a.group_order, 0), a.rowid", (owner["id"], ROOT)):
        parts.setdefault(a["related_id"], []).append(
            {"file_kind": a["file_kind"], "file_name": a["file_name"], "file_size": a["file_size"],
             "group_label": a["group_label"], "group_order": a["group_order"]})
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
async def stream_item(item_id: str, request: Request, part: int = 1, download: bool = False):
    """Faylni Telegram'dan (mahalliy Bot API server orqali) qaytarib olib ko'rsatadi.
    part - elementning barcha fayllari orasidagi tartib raqami (1 dan); download=1 -
    yuklab olish (SRT, audio)."""
    owner = _owner()
    _item(owner, item_id)
    parts = _parts(item_id)
    if not 1 <= part <= len(parts):
        raise HTTPException(404, "Video qismi topilmadi.")
    att = parts[part - 1]
    media_type = {"video": "video/mp4", "audio": "audio/mpeg"}.get(att["file_kind"], "application/octet-stream")
    disposition = {}
    if download:
        name = att["file_name"] or f"fayl_{part}"
        fallback = name.encode("ascii", "replace").decode().replace('"', "_")
        disposition = {"Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"}

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
        response = app.range_file_response(request, source, media_type)
        response.headers.update(disposition)
        return response

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
    passthrough.update(disposition)
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
    parts = None
    if v["kind"] == "split_only":
        # "Video bo'lish"dagi video: tayyor qismlar bo'lsa - qayta bo'linmaydi.
        import app
        if v["split_status"] in ("splitting", "restoring") or v["telegram_send_status"] == "sending":
            raise HTTPException(409, "Video hozir qayta ishlanmoqda - tugashini kuting.")
        path, suffix = v["path"], ""
        parts = app._ready_split_parts(v) or None
        if not parts and not (path and Path(path).exists()):
            raise HTTPException(400, "Video fayli serverda topilmadi.")
    elif variant == "project":
        groups = project_files(v)
        if not groups:
            raise HTTPException(400, "Loyihada yuboriladigan fayl topilmadi.")
        if folder_id:
            _folder(owner, folder_id)
        title = Path(v["original_name"] or "video").stem[:200]
        _start_upload("videos", video_id, groups, title, folder_id or None, remove_from_library, owner)
        return {"ok": True, "files": len(groups)}
    else:
        if variant not in VARIANTS:
            raise HTTPException(400, "Noto'g'ri variant.")
        column, suffix = VARIANTS[variant]
        path = v[column]
        if not path or not Path(path).exists():
            raise HTTPException(400, "Bu variant uchun video fayli serverda yo'q.")
    if folder_id:
        _folder(owner, folder_id)

    title = (Path(v["original_name"] or "video").stem + suffix)[:200]
    _start_upload("videos", video_id, [{"label": None, "kind": "video", "path": path, "parts": parts, "order": 0}],
                  title, folder_id or None, remove_from_library, owner)
    return {"ok": True}


@router.post("/upload-cloud")
async def upload_from_cloud(cloud_file_id: str = Form(...), folder_id: str = Form(""),
                            remove_from_library: bool = Form(True)):
    """Bulutdagi videoni Video Bazaga yuklaydi (Kutubxonaga o'tkazmasdan)."""
    owner = _owner()
    if not IDEA_BOT_TOKEN:
        raise HTTPException(400, "Bot sozlanmagan (IDEA_BOT_TOKEN).")
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND kind = 'video'", (cloud_file_id,))
    if not f or not Path(f["path"]).exists():
        raise HTTPException(404, "Bulutda bunday video topilmadi.")
    if f["bot_upload_status"] == "uploading":
        raise HTTPException(409, "Bu video hozir botga yuklanmoqda.")
    if folder_id:
        _folder(owner, folder_id)
    title = Path(f["original_name"] or "video").stem[:200]
    _start_upload("cloud_files", cloud_file_id, [{"label": None, "kind": "video", "path": f["path"], "order": 0}],
                  title, folder_id or None, remove_from_library, owner)
    return {"ok": True}


def _start_upload(table, record_id, groups, title, folder_id, remove_after, owner):
    db.execute(f"UPDATE {table} SET bot_upload_status = 'uploading', bot_upload_error = NULL, "
               f"bot_upload_progress = '', bot_upload_folder_id = ?, bot_upload_title = ? WHERE id = ?",
               (folder_id, title, record_id))
    asyncio.create_task(_upload_job(record_id, groups, title, folder_id, remove_after,
                                    owner["id"], owner["telegram_chat_id"], table))


SEND_METHODS = {  # fayl turi -> (Bot API metodi, maydon nomi, MIME)
    "video": ("sendVideo", "video", "video/mp4"),
    "audio": ("sendAudio", "audio", "audio/mpeg"),
    "file": ("sendDocument", "document", "application/octet-stream"),
}


async def _upload_job(record_id, groups, title, folder_id, remove_from_library, owner_id, chat_id, table="videos"):
    """groups: [{label, kind, path, order, parts?}] - har bir guruh bitta fayl
    (1.9 GB'dan katta video bir necha qism bo'ladi). Bitta oddiy video uchun
    label=None. table: "videos" (Kutubxona / Video bo'lish) yoki "cloud_files" (Bulut)."""
    import app
    split_root = SPLIT_DIR / f"bot_{record_id}"
    log = (lambda text: db.log_line(record_id, text)) if table == "videos" else (lambda text: None)
    try:
        # Avval katta videolarni qismlarga bo'lamiz - jami fayllar soni ma'lum bo'ladi.
        for g in groups:
            if g.get("parts"):
                continue
            if g["kind"] == "video":
                async with app._SPLIT_LOCK:
                    out = split_root / f"g{g['order']}"
                    shutil.rmtree(out, ignore_errors=True)
                    g["parts"] = await asyncio.get_event_loop().run_in_executor(
                        None, transcription.split_video_by_size, Path(g["path"]), out)
            else:
                g["parts"] = [Path(g["path"])]
        total = sum(len(g["parts"]) for g in groups)
        location = "🎬 Video Baza / " + ideaflow_bot.folder_path(owner_id, folder_id)
        uploaded, done = [], 0
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT) as client:
            for g in groups:
                method, field, mime = SEND_METHODS[g["kind"]]
                n_parts = len(g["parts"])
                source = Path(g["path"])
                for i, part in enumerate(g["parts"], start=1):
                    db.execute(f"UPDATE {table} SET bot_upload_progress = ? WHERE id = ?",
                               (f"{done}/{total}", record_id))
                    label = title + (f" — {g['label']}" if g["label"] else "")
                    if n_parts > 1:
                        label += f" — {i}/{n_parts}-qism"
                    if g["label"] is None:
                        base = title
                    elif g["kind"] == "file":
                        base = source.stem  # SRT/TXT o'z nomi bilan (Dars.uz.srt)
                    else:  # "Dars 5 — O'zbekcha video.mp4"
                        clean_label = re.sub(r"^[^\w]+", "", g["label"]).strip()
                        base = f"{title} — {clean_label}"
                    ext = source.suffix or ".mp4"
                    filename = f"{base}_{i:02d}{ext}" if n_parts > 1 else f"{base}{ext}"
                    body = {"chat_id": chat_id, "caption": f"{label}\n📍 {location}"[:1000]}
                    if g["kind"] == "video":
                        body["supports_streaming"] = "true"
                    with open(part, "rb") as f:
                        resp = await client.post(_bot_url(method), data=body, files={field: (filename, f, mime)})
                    try:
                        data = resp.json()
                    except ValueError:
                        data = {}
                    message = data.get("result") or {}
                    media = message.get(field) or message.get("document")
                    if not data.get("ok") or not media:
                        raise RuntimeError(f"Telegram xatosi ({filename}): {resp.text[:300]}")
                    kind = g["kind"] if message.get(field) and g["kind"] != "file" else "file"
                    uploaded.append((g, media["file_id"], kind, filename, Path(part).stat().st_size))
                    done += 1

        if folder_id and not db.fetchone("SELECT 1 FROM idea_folders WHERE id = ?", (folder_id,)):
            folder_id = None  # yuklash davomida papka botda o'chirilgan bo'lsa
        now = db.now()
        item_id = db.new_id()
        db.execute("INSERT INTO idea_items (id, user_id, folder_id, root_type, type, title, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, 'video', ?, ?, ?)", (item_id, owner_id, folder_id, ROOT, title, now, now))
        for g, file_id, kind, filename, size in uploaded:
            db.execute("INSERT INTO idea_attachments (id, user_id, related_type, related_id, file_kind, file_name, "
                       "file_size, telegram_file_id, group_label, group_order, created_at) "
                       "VALUES (?, ?, 'item', ?, ?, ?, ?, ?, ?, ?, ?)",
                       (db.new_id(), owner_id, item_id, kind, filename, size, file_id, g["label"], g["order"], now))
        if table == "videos":
            db.execute("UPDATE videos SET bot_upload_status = 'done', bot_upload_progress = ?, bot_item_id = ? "
                       "WHERE id = ?", (f"{total}/{total}", item_id, record_id))
        else:
            db.execute("UPDATE cloud_files SET bot_upload_status = 'done', bot_upload_progress = ? WHERE id = ?",
                       (f"{total}/{total}", record_id))
        log(f"Botdagi Video Bazaga yuklandi ({len(groups)} fayl, {total} xabar): {location}")
        if remove_from_library:
            if table == "videos":
                v = db.fetchone("SELECT * FROM videos WHERE id = ?", (record_id,))
                if v:
                    app.delete_video_completely(v)
            else:
                shutil.rmtree(Path(groups[0]["path"]).parent, ignore_errors=True)
                db.execute("DELETE FROM cloud_files WHERE id = ?", (record_id,))
    except Exception as e:
        traceback.print_exc()
        db.execute(f"UPDATE {table} SET bot_upload_status = 'error', bot_upload_error = ? WHERE id = ?",
                   (str(e)[:500], record_id))
        log(f"Botga yuklashda xato: {e}")
    finally:
        shutil.rmtree(split_root, ignore_errors=True)


def recover_interrupted_uploads():
    for table in ("videos", "cloud_files"):
        db.execute(f"UPDATE {table} SET bot_upload_status = 'error', bot_upload_error = ? "
                   "WHERE bot_upload_status = 'uploading'",
                   ("Server qayta ishga tushdi - yuklash to'xtab qoldi. Qayta urinib ko'ring.",))
