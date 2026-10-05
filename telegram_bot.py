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
import shutil
import traceback
from pathlib import Path

import httpx

import database as db
import transcription
from storage import CLOUD_DIR, INBOUND_BOT_TOKEN, LOCAL_BOT_API_URL, safe_name, has_space_for

POLL_TIMEOUT = 30
GET_FILE_TIMEOUT = 2 * 60 * 60
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


def _copy_local_bot_file(file_path: str, dest_path: Path) -> int | None:
    """Copy a file returned by telegram-bot-api in ``--local`` mode.

    In local mode ``getFile`` returns an absolute path on this same server.  Using
    that path avoids downloading the file a second time through the Bot API HTTP
    endpoint.  ``None`` means the response was not a usable local path and the
    caller should use the HTTP fallback.
    """
    source = Path(file_path)
    try:
        is_usable = source.is_absolute() and source.is_file()
    except OSError:
        is_usable = False
    if not is_usable:
        return None

    try:
        shutil.copyfile(source, dest_path)
        return dest_path.stat().st_size
    except OSError as exc:
        # The Bot API service may run as another OS user. If its cache path is
        # not readable, leave the existing HTTP endpoint as a safe fallback.
        dest_path.unlink(missing_ok=True)
        print(f"[telegram_bot] Lokal faylni bevosita ko'chirish imkoni bo'lmadi: {exc}. "
              "HTTP zaxira yo'li ishlatiladi.", flush=True)
        return None


async def _send_message(client: httpx.AsyncClient, chat_id, text: str):
    if chat_id is None:
        return
    try:
        await client.post(_api_url("sendMessage"), data={"chat_id": chat_id, "text": text[:4000]})
    except Exception:
        pass


def hide_token(text: str, token: str) -> str:
    """Xato matnida (httpx URL'ni ham yozadi) bot tokeni chatga chiqib ketmasin."""
    return text.replace(token, "***") if token else text


async def download_to_cloud(client: httpx.AsyncClient, file_id: str, original_name: str, notify,
                            size_hint: int = 0, api_base: str = None, token: str = None):
    """Telegram'dagi videoni yuklab olib "Bulut"ga (cloud_files) qo'shadi.

    Shu modulning kiruvchi boti ham, Idea Flow botining "☁️ Webga yuklash"
    bo'limi ham ishlatadi - shuning uchun Bot API manzili va tokeni parametr.
    ``notify(text)`` - foydalanuvchiga xabar (joy yetishmasa). Muvaffaqiyatda
    Bulutdagi fayl nomini, aks holda None qaytaradi."""
    api_base = (api_base or LOCAL_BOT_API_URL).rstrip("/")
    token = token or INBOUND_BOT_TOKEN
    if size_hint and not has_space_for(size_hint):
        await notify(f'"{original_name}" qabul qilinmadi - serverda joy yetarli emas.')
        return None

    # Katta video uchun lokal bot-api serveri getFile so'rovi davomida faylni
    # Telegram'dan yuklab oladi. 2 GB fayl sekin ulanishda bir necha daqiqadan
    # ko'proq vaqt olishi mumkin, shuning uchun qisqa timeout bilan uzmaymiz.
    resp = await client.post(
        f"{api_base}/bot{token}/getFile", data={"file_id": file_id}, timeout=GET_FILE_TIMEOUT
    )
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    name = safe_name(original_name or Path(file_path).name or "video.mp4")
    cloud_id = db.new_id()
    dest_dir = CLOUD_DIR / cloud_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name

    # --local rejimida getFile shu serverdagi tayyor faylning mutlaq yo'lini
    # qaytaradi. Uni HTTP orqali yana bir marta yuklab o'tirmay, disk ichida
    # bevosita doimiy xotiraga ko'chiramiz.
    total = await asyncio.to_thread(_copy_local_bot_file, file_path, dest_path)
    last_local_error: Exception | None = None
    if total is not None:
        print(f"[telegram_bot] Lokal Bot API fayli bevosita xotiraga ko'chirildi: "
              f"{total / (1024 * 1024):.1f} MB", flush=True)
    else:
        # Lokal mutlaq yo'l mavjud bo'lmasa eski HTTP yo'li zaxira bo'lib qoladi.
        # Fayl hali tayyor bo'lmasa bir necha marta kutib qayta urinamiz.
        local_url = f"{api_base}/file/bot{token}/{file_path}"
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
        pub_resp = await client.post(f"{PUBLIC_API_BASE}/bot{token}/getFile",
                                      data={"file_id": file_id}, timeout=60)
        pub_resp.raise_for_status()
        pub_file_path = pub_resp.json()["result"]["file_path"]
        public_url = f"{PUBLIC_API_BASE}/file/bot{token}/{pub_file_path}"
        total = await _stream_to_file(client, public_url, dest_path)

    if not has_space_for(0):
        dest_path.unlink(missing_ok=True)
        try:
            dest_dir.rmdir()
        except OSError:
            pass
        await notify(f'"{name}" qabul qilinmadi - serverda joy yetarli emas.')
        return None

    admin = db.fetchone("SELECT id FROM users WHERE role = 'superadmin' ORDER BY created_at LIMIT 1")
    if not admin:
        dest_path.unlink(missing_ok=True)
        await notify('Super-admin hisobi topilmadi.')
        return None
    db.execute(
        """INSERT INTO cloud_files (id, kind, original_name, filename, path, file_size, created_at, owner_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (cloud_id, "video", name, dest_path.name, str(dest_path), total, db.now(), admin["id"]),
    )
    try:
        thumb_path = dest_dir / "thumb.jpg"
        loop = asyncio.get_event_loop()
        has_thumb = await loop.run_in_executor(None, transcription.generate_thumbnail, dest_path, thumb_path)
        if has_thumb:
            db.execute("UPDATE cloud_files SET thumbnail_path = ? WHERE id = ?", (str(thumb_path), cloud_id))
    except Exception:
        pass
    return name


async def _download_and_save_to_cloud(client: httpx.AsyncClient, file_id: str, original_name: str,
                                      chat_id, size_hint: int = 0) -> None:
    async def notify(text):
        await _send_message(client, chat_id, text)

    name = await download_to_cloud(client, file_id, original_name, notify, size_hint=size_hint)
    if name:
        await notify(f'"{name}" qabul qilindi va Bulutga yuklandi (saytdagi "Bulut" bo\'limida ko\'rasiz).')


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
        await _send_message(client, chat_id, f"Video qabul qilishda xato: {hide_token(str(e), INBOUND_BOT_TOKEN)}")


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
