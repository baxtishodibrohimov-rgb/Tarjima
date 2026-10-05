"""Bulutdagi zip fayllar: ichini ko'rish, kerakli fayllarni Bulutga chiqarish
va bitta faylni zipdan to'g'ridan-to'g'ri yuklab olish.

Zip butunligicha ochilmaydi: Python'ning zipfile moduli faqat zip oxiridagi
mundarijani o'qiydi va tanlangan faylni oqim (stream) bilan ko'chiradi -
20-30 GB'lik zip ham xotiraga yuklanmaydi. Chiqarilgan har bir fayl Bulutda
alohida yozuv bo'ladi (video -> kind='video', qolganlari -> kind='file'),
shuning uchun keyin odatdagidek "Video bo'lish"/Kutubxonaga o'tkaziladi.
Xavfsizlik: zip ichidagi yo'llar ishlatilmaydi (faqat fayl nomi olinadi), shu
sababli "../../" kabi yo'llar server fayllariga yoza olmaydi.
"""
import asyncio
import mimetypes
import shutil
import traceback
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

import auth
import database as db
from storage import CLOUD_DIR, has_space_for, safe_name

router = APIRouter(prefix="/api/cloud-files")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".mts", ".flv", ".wmv", ".mpg",
              ".mpeg", ".3gp"}
COPY_CHUNK = 1024 * 1024
_JUNK_DIRS = {"__MACOSX"}
_JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def entry_kind(name: str) -> str:
    return "video" if Path(name).suffix.lower() in VIDEO_EXTS else "file"


def entry_name(info: zipfile.ZipInfo) -> str:
    """Windows'da yaratilgan zip'larda nomlar UTF-8 belgisiz yoziladi va
    zipfile ularni cp437 deb o'qiydi - kirill harflari buziladi. Asl baytlarni
    tiklab, avval UTF-8, so'ng cp866 (rus Windows) sifatida o'qiymiz."""
    name = info.filename
    if not info.flag_bits & 0x800:
        raw = name.encode("cp437", errors="replace")
        for encoding in ("utf-8", "cp866"):
            try:
                name = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    return name.replace("\\", "/")


def bulut_name(path: str) -> str:
    """Bulutdagi nom: papka nomi + fayl nomi ("2-modul - 1-dars.mp4"), aks holda
    turli modullardagi "1-dars.mp4"lar bir-biridan farqlanmaydi."""
    parts = [p for p in path.split("/") if p not in ("", ".", "..")]
    name = " - ".join(parts[-2:]) if len(parts) > 1 else (parts[0] if parts else "file")
    return safe_name(name)


def _is_junk(name: str) -> bool:
    parts = name.split("/")
    return bool(_JUNK_DIRS.intersection(parts)) or parts[-1] in _JUNK_NAMES


def list_entries(path: Path) -> list:
    with zipfile.ZipFile(path) as zf:
        entries = []
        for index, info in enumerate(zf.infolist()):
            name = entry_name(info)
            if info.is_dir() or _is_junk(name):
                continue
            entries.append({"index": index, "path": name, "name": Path(name).name, "size": info.file_size,
                            "kind": entry_kind(name), "encrypted": bool(info.flag_bits & 0x1)})
        return entries


def on_zip_added(cloud_id: str, path: Path):
    """Yangi zip Bulutga tushganda mundarijasini o'qib, fayllar sonini saqlaydi."""
    try:
        entries = list_entries(path)
        db.execute("UPDATE cloud_files SET zip_entry_count = ?, zip_total_size = ?, extract_status = 'none', "
                   "extract_error = NULL WHERE id = ?",
                   (len(entries), sum(e["size"] for e in entries), cloud_id))
    except (zipfile.BadZipFile, OSError) as e:
        db.execute("UPDATE cloud_files SET extract_status = 'error', extract_error = ? WHERE id = ?",
                   (f"Zip faylni o'qib bo'lmadi (buzilgan yoki zip emas): {e}"[:500], cloud_id))


def recover_interrupted():
    db.execute("UPDATE cloud_files SET extract_status = 'error', extract_error = ? "
               "WHERE extract_status = 'extracting'",
               ("Server qayta ishga tushdi - chiqarish to'xtab qoldi. Qolgan fayllarni qayta tanlang.",))


def _zip_file(cloud_id: str) -> dict:
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND kind = 'zip' AND owner_id = ?",
                    (cloud_id, auth.current_user_id()))
    if not f or not Path(f["path"]).exists():
        raise HTTPException(404, "Bulutda bunday zip topilmadi.")
    return f


@router.get("/{cloud_id}/zip")
async def zip_contents(cloud_id: str):
    f = _zip_file(cloud_id)
    try:
        entries = await asyncio.to_thread(list_entries, Path(f["path"]))
    except zipfile.BadZipFile as e:
        raise HTTPException(400, f"Zip faylni o'qib bo'lmadi: {e}")
    return {"id": f["id"], "name": f["original_name"], "entries": entries,
            "extract_status": f["extract_status"] or "none"}


@router.post("/{cloud_id}/zip/extract")
async def zip_extract(cloud_id: str, request: Request):
    """Body: {"indices": [..]} yoki {"mode": "all"|"videos"}, ixtiyoriy
    {"delete_zip": true} - chiqarilgach zipni o'chirish."""
    f = _zip_file(cloud_id)
    if f["extract_status"] == "extracting":
        raise HTTPException(409, "Bu zipdan hozir fayllar chiqarilmoqda - tugashini kuting.")
    body = await request.json()
    entries = await asyncio.to_thread(list_entries, Path(f["path"]))
    mode = body.get("mode")
    if mode == "all":
        chosen = entries
    elif mode == "videos":
        chosen = [e for e in entries if e["kind"] == "video"]
    else:
        wanted = {int(i) for i in body.get("indices") or []}
        chosen = [e for e in entries if e["index"] in wanted]
    if not chosen:
        raise HTTPException(400, "Chiqarish uchun fayl tanlanmagan." if mode != "videos"
                            else "Zip ichida video topilmadi.")
    locked = [e["name"] for e in chosen if e["encrypted"]]
    if locked:
        raise HTTPException(400, f"Parol bilan himoyalangan fayllar ochilmaydi: {', '.join(locked[:5])}")
    total = sum(e["size"] for e in chosen)
    user = auth.current_user()
    if not has_space_for(total) or not auth.has_user_space(total, user):
        raise HTTPException(400, f"Joy yetarli emas: chiqarish uchun {total / 1024 ** 3:.2f} GB kerak.")

    db.execute("UPDATE cloud_files SET extract_status = 'extracting', extract_progress = ?, extract_error = NULL "
               "WHERE id = ?", (f"0/{len(chosen)}", cloud_id))
    asyncio.create_task(_extract_job(cloud_id, [e["index"] for e in chosen], user["id"],
                                     bool(body.get("delete_zip"))))
    return {"ok": True, "count": len(chosen), "total_size": total}


async def _extract_job(cloud_id: str, indices: list, owner_id: str, delete_zip: bool):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ?", (cloud_id,))
    done = 0
    try:
        done = await asyncio.to_thread(_extract_sync, cloud_id, Path(f["path"]), indices, owner_id)
    except Exception as e:
        traceback.print_exc()
        prefix = f"{done} ta fayl chiqarildi, keyin xato: " if done else ""
        db.execute("UPDATE cloud_files SET extract_status = 'error', extract_error = ? WHERE id = ?",
                   ((prefix + str(e))[:500], cloud_id))
        return
    if delete_zip:
        shutil.rmtree(Path(f["path"]).parent, ignore_errors=True)
        db.execute("DELETE FROM cloud_files WHERE id = ?", (cloud_id,))
    else:
        db.execute("UPDATE cloud_files SET extract_status = 'done', extract_progress = ? WHERE id = ?",
                   (f"{len(indices)}/{len(indices)}", cloud_id))


def _extract_sync(cloud_id: str, zip_path: Path, indices: list, owner_id: str) -> int:
    import app  # aylana importdan qochish uchun (thumbnail yordamchisi app.py'da)
    done = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for index in indices:
            info = infos[index]
            name = bulut_name(entry_name(info))
            new_id = db.new_id()
            dest_dir = CLOUD_DIR / new_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / name
            try:
                with zf.open(info) as src, dest_path.open("wb") as out:
                    shutil.copyfileobj(src, out, COPY_CHUNK)
            except Exception:
                shutil.rmtree(dest_dir, ignore_errors=True)
                raise
            kind = entry_kind(name)
            db.execute("INSERT INTO cloud_files (id, kind, original_name, filename, path, file_size, created_at, "
                       "owner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (new_id, kind, name, dest_path.name, str(dest_path), dest_path.stat().st_size, db.now(),
                        owner_id))
            if kind == "video":
                app._generate_cloud_thumbnail(new_id, dest_dir, dest_path)
            done += 1
            db.execute("UPDATE cloud_files SET extract_progress = ? WHERE id = ?",
                       (f"{done}/{len(indices)}", cloud_id))
    return done


def _attachment_header(name: str) -> str:
    fallback = name.encode("ascii", "replace").decode().replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


@router.get("/{cloud_id}/zip/download")
def zip_download_entry(cloud_id: str, index: int):
    """Zip ichidagi bitta faylni chiqarmasdan, to'g'ridan-to'g'ri yuklab berish."""
    f = _zip_file(cloud_id)
    zf = zipfile.ZipFile(f["path"])
    try:
        info = zf.infolist()[index]
    except IndexError:
        zf.close()
        raise HTTPException(404, "Zip ichida bunday fayl yo'q.")
    if info.is_dir() or info.flag_bits & 0x1:
        zf.close()
        raise HTTPException(400, "Bu faylni yuklab bo'lmaydi (papka yoki parolli fayl).")
    name = Path(entry_name(info)).name

    def stream():
        with zf, zf.open(info) as src:
            while chunk := src.read(COPY_CHUNK):
                yield chunk

    return StreamingResponse(stream(), media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                             headers={"Content-Disposition": _attachment_header(name),
                                      "Content-Length": str(info.file_size)})


@router.get("/{cloud_id}/download")
async def cloud_file_download(cloud_id: str):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND owner_id = ?", (cloud_id, auth.current_user_id()))
    if not f or not Path(f["path"]).exists():
        raise HTTPException(404, "Fayl topilmadi.")
    return FileResponse(f["path"], filename=f["original_name"] or Path(f["path"]).name)
