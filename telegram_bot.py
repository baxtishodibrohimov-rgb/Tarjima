"""Telegram bot orqali video qabul qilish (kiruvchi yo'nalish).

app.py'dagi mavjud _send_video_file_to_telegram funksiyasi faqat CHIQUVCHI (tayyor
videoni Telegram'ga yuborish) yo'nalishda ishlaydi va TELEGRAM_BOT_TOKEN'ni ishlatadi.
Bu modul esa TESKARI yo'nalishni - istalgan foydalanuvchi botga video (yoki video
sifatida yuborilgan hujjat) yuborsa, uni avtomatik yuklab olib, "Bulut" umumiy fayl
saqlagichiga (cloud_files, kind='video') qo'shadi - xuddi saytdan "Tarjima -> Video
yuklash" orqali yuklangandek. Shundan keyin foydalanuvchi saytdan "Kelgan videolarni
ko'rish"da uni kerakli bo'limga qo'shadi.

ATAYLAB TELEGRAM_BOT_TOKEN'dan ALOHIDA, o'ziga xos INBOUND_BOT_TOKEN ishlatadi -
chunki TELEGRAM_BOT_TOKEN allaqachon boshqa tashqi tizim (Idea Flow) tomonidan
kuzatilmoqda, va Telegram'da bitta bot tokenini bir vaqtning o'zida faqat bitta
tizim getUpdates orqali ishonchli tinglashi mumkin.

Long polling (getUpdates) ishlatiladi - webhook uchun ochiq (public) URL sozlash shart
emas. O'z-o'zini joylashtirgan Bot API server (LOCAL_BOT_API_URL) ishlatiladi - bu
katta video fayllarni ham (2 GB gacha) yuklab olishga imkon beradi.
"""
import asyncio
import traceback
from pathlib import Path

import httpx

import database as db
import transcription
from storage import CLOUD_DIR, INBOUND_BOT_TOKEN, LOCAL_BOT_API_URL, safe_name, has_space_for

POLL_TIMEOUT = 30
LAST_UPDATE_ID_KEY = "telegram_inbound_bot_last_update_id"

# Ochiq (public) Telegram Cloud API - o'z-o'zini joylashtirgan server fayl xizmat
# qilishda muammo bersa (masalan 404), zaxira sifatida ishlatiladi. Diqqat: bu yerda
# 20 MB'dan katta fayllar ISHLAMAYDI (Telegram Cloud API'ning o'zining cheklovi) -
# shuning uchun bu faqat zaxira, asosiy yo'l emas.
PUBLIC_API_BASE = "https://api.telegram.org"


def _api_url(method: str) -> str:
    return f"{LOCAL_BOT_API_URL.rstrip('/')}/bot{INBOUND_BOT_TOKEN}/{method}"


async def _stream_to_file(client: httpx.AsyncClient, url: str, dest_path: Path) -> int:
    total = 0
    async with client.stream("GET", url, timeout=None) as stream:
        stream.raise_for_status()
        with dest_path.open("wb") as f:
            async for part in stream.aiter_bytes(1024 * 1024):
                f.write(part)
                total += len(part)
    return total


async def _send_message(client: httpx.AsyncClient, chat_id, text: str):
    if chat_id is None:
        return
    try:
        await client.post(_api_url("sendMessage"), data={"chat_id": chat_id, "text": text[:4000]})
    except Exception:
        pass


async def _download_and_save_to_cloud(client: httpx.AsyncClient, file_id: str, original_name: str,
                                        chat_id, size_hint: int = 0) -> None:
    if size_hint and not has_space_for(size_hint):
        await _send_message(client, chat_id, f'"{original_name}" qabul qilinmadi - serverda joy yetarli emas.')
        return

    # Katta video uchun lokal bot-api serveri getFile so'rovi davomida faylni
    # Telegram'dan orqa fonda yuklab olishi mumkin - shuning uchun uzunroq timeout.
    resp = await client.post(_api_url("getFile"), data={"file_id": file_id}, timeout=180)
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    name = safe_name(original_name or Path(file_path).name or "video.mp4")
    cloud_id = db.new_id()
    dest_dir = CLOUD_DIR / cloud_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name

    # Katta fayllarni Telegram'dan lokal bot-api serveriga ko'chirish biroz vaqt olishi
    # mumkin - shu vaqt ichida fayl hali "tayyor emas" (404) bo'lishi mumkin, shuning
    # uchun darhol zaxira usulga o'tish o'rniga bir necha marta kutib qayta urinamiz.
    local_url = f"{LOCAL_BOT_API_URL.rstrip('/')}/file/bot{INBOUND_BOT_TOKEN}/{file_path}"
    last_local_error: Exception | None = None
    total = None
    for attempt in range(6):
        try:
            total = await _stream_to_file(client, local_url, dest_path)
            last_local_error = None
            break
        except httpx.HTTPStatusError as e:
            last_local_error = e
            if e.response.status_code == 404 and attempt < 5:
                print(f"[telegram_bot] Fayl hali lokal serverda tayyor emas (404), "
                      f"{attempt + 1}-urinish, 3s kutib qayta urinilmoqda...", flush=True)
                await asyncio.sleep(3)
                continue
            break

    if last_local_error is not None:
        # O'z-o'zini joylashtirgan server bir necha urinishdan keyin ham fayl xizmat
        # qila olmadi - ochiq Telegram Cloud API orqali qayta urinamiz (faqat <=20 MB
        # fayllar uchun ishlaydi).
        print(f"[telegram_bot] Lokal bot-api serveridan yuklab bo'lmadi "
              f"({last_local_error.response.status_code}), ochiq Telegram API orqali "
              f"qayta urinilmoqda...", flush=True)
        pub_resp = await client.post(f"{PUBLIC_API_BASE}/bot{INBOUND_BOT_TOKEN}/getFile",
                                      data={"file_id": file_id}, timeout=60)
        pub_resp.raise_for_status()
        pub_file_path = pub_resp.json()["result"]["file_path"]
        public_url = f"{PUBLIC_API_BASE}/file/bot{INBOUND_BOT_TOKEN}/{pub_file_path}"
        total = await _stream_to_file(client, public_url, dest_path)

    if not has_space_for(0):
        dest_path.unlink(missing_ok=True)
        try:
            dest_dir.rmdir()
        except OSError:
            pass
        await _send_message(client, chat_id, f'"{name}" qabul qilinmadi - serverda joy yetarli emas.')
        return

    db.execute(
        """INSERT INTO cloud_files (id, kind, original_name, filename, path, file_size, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cloud_id, "video", name, dest_path.name, str(dest_path), total, db.now()),
    )
    try:
        thumb_path = dest_dir / "thumb.jpg"
        loop = asyncio.get_event_loop()
        has_thumb = await loop.run_in_executor(None, transcription.generate_thumbnail, dest_path, thumb_path)
        if has_thumb:
            db.execute("UPDATE cloud_files SET thumbnail_path = ? WHERE id = ?", (str(thumb_path), cloud_id))
    except Exception:
        pass
    await _send_message(client, chat_id, f'"{name}" qabul qilindi va Bulutga yuklandi ('
                                          f'saytda "Tarjima -> Kelgan videolarni ko\'rish"dan ko\'rasiz).')


async def _handle_update(client: httpx.AsyncClient, update: dict):
    message = update.get("message") or update.get("channel_post")
    if not message:
        return
    chat_id = message.get("chat", {}).get("id")
    video = message.get("video")
    document = message.get("document")
    print(f"[telegram_bot] Xabar qabul qilindi: chat_id={chat_id}, "
          f"video={'ha' if video else 'yoq'}, document={'ha' if document else 'yoq'}"
          f"{' (' + (document or {}).get('mime_type', '') + ')' if document else ''}", flush=True)

    try:
        if video:
            name = video.get("file_name") or f"{video['file_id']}.mp4"
            print(f"[telegram_bot] Video yuklab olinmoqda: {name}", flush=True)
            await _download_and_save_to_cloud(client, video["file_id"], name, chat_id,
                                               size_hint=video.get("file_size") or 0)
            print(f"[telegram_bot] Video saqlandi: {name}", flush=True)
        elif document and (document.get("mime_type") or "").startswith("video/"):
            name = document.get("file_name") or f"{document['file_id']}.mp4"
            print(f"[telegram_bot] Hujjat (video) yuklab olinmoqda: {name}", flush=True)
            await _download_and_save_to_cloud(client, document["file_id"], name, chat_id,
                                               size_hint=document.get("file_size") or 0)
            print(f"[telegram_bot] Hujjat (video) saqlandi: {name}", flush=True)
        else:
            print("[telegram_bot] Xabarda video/video-hujjat topilmadi, e'tiborsiz qoldirildi.", flush=True)
    except Exception as e:
        print(f"[telegram_bot] XATO video qabul qilishda: {e}", flush=True)
        traceback.print_exc()
        await _send_message(client, chat_id, f"Video qabul qilishda xato: {e}")


async def poll_updates():
    """Cheksiz tsikl - Telegram'dan yangi xabarlarni so'rab turadi (long polling).
    Faqat INBOUND_BOT_TOKEN sozlangan bo'lsa ishga tushadi."""
    if not INBOUND_BOT_TOKEN:
        print("[telegram_bot] INBOUND_BOT_TOKEN sozlanmagan - kiruvchi video qabul qilish o'chirilgan.", flush=True)
        return

    print("[telegram_bot] Ishga tushirilmoqda...", flush=True)
    try:
        async with httpx.AsyncClient(timeout=30) as check_client:
            me_resp = await check_client.post(_api_url("getMe"))
            me_resp.raise_for_status()
            me = me_resp.json().get("result", {})
            print(f"[telegram_bot] Ulandi: @{me.get('username')} (id={me.get('id')}, "
                  f"ism='{me.get('first_name')}')", flush=True)
    except Exception as e:
        print(f"[telegram_bot] XATO: botga ulanib bo'lmadi (token/LOCAL_BOT_API_URL tekshiring): {e}", flush=True)
        traceback.print_exc()

    offset = int(db.get_setting(LAST_UPDATE_ID_KEY, "0") or "0")
    print(f"[telegram_bot] Polling boshlandi (offset={offset}).", flush=True)
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as client:
        while True:
            try:
                resp = await client.post(_api_url("getUpdates"), data={
                    "offset": offset, "timeout": POLL_TIMEOUT,
                    "allowed_updates": '["message","channel_post"]',
                })
                resp.raise_for_status()
                updates = resp.json().get("result", [])
                if updates:
                    print(f"[telegram_bot] {len(updates)} ta yangi xabar keldi.", flush=True)
                for u in updates:
                    offset = u["update_id"] + 1
                    db.set_setting(LAST_UPDATE_ID_KEY, str(offset))
                    await _handle_update(client, u)
            except Exception:
                print("[telegram_bot] XATO polling tsiklida:", flush=True)
                traceback.print_exc()
                await asyncio.sleep(5)
