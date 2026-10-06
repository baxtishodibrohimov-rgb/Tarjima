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
import copy
import mimetypes
import shutil
import struct
import traceback
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
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


def _unicode_path_extra(info: zipfile.ZipInfo):
    """Info-ZIP "Unicode Path" qo'shimcha maydoni (0x7075) - UTF-8 nom.
    Windows "Siqilgan papka", WinRAR va boshqalar ruscha nomni asosiy maydonga
    "???" qilib yozib, to'g'ri nomni shu yerga qo'yadi; Python zipfile uni o'qimaydi."""
    extra = info.extra or b""
    pos = 0
    while pos + 4 <= len(extra):
        header_id, size = struct.unpack("<HH", extra[pos:pos + 4])
        data = extra[pos + 4:pos + 4 + size]
        pos += 4 + size
        if header_id != 0x7075 or len(data) < 6 or data[0] != 1:
            continue
        raw_header = info.filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437", errors="replace")
        crc_ok = struct.unpack("<I", data[1:5])[0] == zlib.crc32(raw_header)
        try:
            name = data[5:].decode("utf-8")
        except UnicodeDecodeError:
            return None
        # CRC mos kelmasa (nomni boshqa dastur o'zgartirgan) - faqat asosiy nom
        # "?" bilan buzilgan bo'lsa ishlatamiz.
        return name if crc_ok or "?" in info.filename else None
    return None


def _local_header(zf: zipfile.ZipFile, info: zipfile.ZipInfo):
    """Faylning o'z (lokal) sarlavhasi: (bayroqlar, nom baytlari) yoki None.
    Ba'zi dasturlar markaziy ro'yxatga "???" yozib, to'g'ri nomni (cp866) faqat
    shu yerda qoldiradi - Windows Explorer nomni shu yerdan o'qiydi."""
    try:
        zf.fp.seek(info.header_offset)
        header = zf.fp.read(30)
        if len(header) < 30 or header[:4] != b"PK\x03\x04":
            return None
        flags = struct.unpack("<H", header[6:8])[0]
        name_len = struct.unpack("<H", header[26:28])[0]
        return flags, zf.fp.read(name_len)
    except (OSError, AttributeError, ValueError, struct.error):
        return None


def open_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo):
    """zf.open() markaziy va lokal nomlar mos kelmasa BadZipFile beradi (yuqoridagi
    "???" holati). Mazmun bir xil - shuning uchun lokal nomni kutilgan nom qilib ochamiz."""
    local = _local_header(zf, info)
    if local:
        flags, raw = local
        expected = raw.decode("utf-8" if flags & 0x800 else "cp437", errors="replace")
        if expected != info.orig_filename:
            info = copy.copy(info)
            info.orig_filename = expected
    return zf.open(info)


def entry_name(info: zipfile.ZipInfo, zf: zipfile.ZipFile = None) -> str:
    """Windows'da yaratilgan zip'larda nomlar UTF-8 belgisiz yoziladi va
    zipfile ularni cp437 deb o'qiydi - kirill harflari buziladi. Tartib:
    Unicode qo'shimcha maydoni; markaziy nom "???" bo'lsa - lokal sarlavhadagi
    nom; so'ng asl baytlar UTF-8, keyin cp866 (rus Windows) sifatida."""
    unicode_name = _unicode_path_extra(info)
    if unicode_name:
        return unicode_name.replace("\\", "/")
    name = info.filename
    if not info.flag_bits & 0x800:
        raw = name.encode("cp437", errors="replace")
        if b"?" in raw and zf is not None:
            local = _local_header(zf, info)
            if local and b"?" not in local[1]:
                raw = local[1]
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
            name = entry_name(info, zf)
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
            name = bulut_name(entry_name(info, zf))
            new_id = db.new_id()
            dest_dir = CLOUD_DIR / new_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / name
            try:
                with open_entry(zf, info) as src, dest_path.open("wb") as out:
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
    name = Path(entry_name(info, zf)).name

    def stream():
        with zf, open_entry(zf, info) as src:
            while chunk := src.read(COPY_CHUNK):
                yield chunk

    return StreamingResponse(stream(), media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                             headers={"Content-Disposition": _attachment_header(name),
                                      "Content-Length": str(info.file_size)})


@router.post("/{cloud_id}/rename")
async def cloud_file_rename(cloud_id: str, name: str = Form(...)):
    """Bulutdagi fayl nomini o'zgartirish (masalan, zipdan "???" bo'lib chiqqan nom).
    Faqat ko'rinadigan nom o'zgaradi; kengaytma (.mp4, .zip) saqlanadi."""
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND owner_id = ?", (cloud_id, auth.current_user_id()))
    if not f:
        raise HTTPException(404, "Fayl topilmadi.")
    if not name.strip(" ._"):
        raise HTTPException(400, "Nom bo'sh bo'lmasin.")
    new = safe_name(name.strip())[:200]
    ext = Path(f["original_name"] or "").suffix
    if ext and not new.lower().endswith(ext.lower()):
        new += ext
    db.execute("UPDATE cloud_files SET original_name = ? WHERE id = ?", (new, cloud_id))
    return {"ok": True, "original_name": new}


@router.get("/{cloud_id}/download")
async def cloud_file_download(cloud_id: str):
    f = db.fetchone("SELECT * FROM cloud_files WHERE id = ? AND owner_id = ?", (cloud_id, auth.current_user_id()))
    if not f or not Path(f["path"]).exists():
        raise HTTPException(404, "Fayl topilmadi.")
    return FileResponse(f["path"], filename=f["original_name"] or Path(f["path"]).name)
