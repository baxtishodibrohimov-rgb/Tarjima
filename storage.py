"""
Persistent storage layout.

Barcha doimiy ma'lumot STORAGE_DIR ichida saqlanadi (Railway persistent volume
shu papkaga ulanishi kerak). Source-kod papkasiga hech narsa yozilmaydi.
"""
import os
import shutil
from pathlib import Path

STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", "./storage")).resolve()

DB_DIR = STORAGE_DIR / "database"
VIDEOS_DIR = STORAGE_DIR / "videos"
CHUNKS_DIR = STORAGE_DIR / "chunks"
RESULTS_DIR = STORAGE_DIR / "results"
TTS_DIR = STORAGE_DIR / "tts"
UPLOADS_DIR = STORAGE_DIR / "uploads"
SPLIT_DIR = STORAGE_DIR / "split_tmp"
CLOUD_DIR = STORAGE_DIR / "cloud"

for d in (DB_DIR, VIDEOS_DIR, CHUNKS_DIR, RESULTS_DIR, TTS_DIR, UPLOADS_DIR, SPLIT_DIR, CLOUD_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DB_DIR / "app.db"
SECRET_KEY_PATH = DB_DIR / ".secret_key"

# ---------------------------------------------------------------------------
# Konfiguratsiya (environment variable orqali boshqariladi)
# ---------------------------------------------------------------------------
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", "300"))
MAX_WHISPER_CONCURRENCY = int(os.environ.get("MAX_WHISPER_CONCURRENCY", "4"))
MAX_ACTIVE_VIDEO_JOBS = int(os.environ.get("MAX_ACTIVE_VIDEO_JOBS", "1"))
MAX_ACTIVE_TTS_JOBS = int(os.environ.get("MAX_ACTIVE_TTS_JOBS", "1"))
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(20 * 1024 * 1024 * 1024)))  # 20 GB
STORAGE_LIMIT = int(os.environ.get("STORAGE_LIMIT", str(95 * 1024 * 1024 * 1024)))  # 95 GB
REPETITION_THRESHOLD = int(os.environ.get("REPETITION_THRESHOLD", "3"))
UPLOAD_CHUNK_SIZE = int(os.environ.get("UPLOAD_CHUNK_SIZE", str(8 * 1024 * 1024)))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")  # bo'sh bo'lsa himoya o'chirilgan
# Idea Flow (Lovable) botidagi "Video Baza / Tarjima" papkasiga tayyor videolarni
# yozib qo'yish uchun - ikkala tomonda ham bir xil bo'lishi shart.
DARSLIK_API_KEY = os.environ.get("DARSLIK_API_KEY", "")
IDEA_FLOW_URL = os.environ.get("IDEA_FLOW_URL", "")  # masalan https://xxxx.lovable.app

# Telegram'ga videoning O'ZINI (havola emas) yuborish uchun - o'z-o'zini
# joylashtirgan (self-hosted) Bot API server orqali, chunki oddiy
# api.telegram.org 50 MB bilan cheklaydi, bu server esa 2 GB gacha ruxsat beradi.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "http://telegram-bot-api.railway.internal:8081")

# Kiruvchi videolarni ("Bulut"ga avtomatik yuklash) qabul qiladigan bot - ATAYLAB
# yuqoridagi TELEGRAM_BOT_TOKEN'dan ALOHIDA bot bo'lishi shart. Sabab: Telegram'da
# bitta bot tokenini bir vaqtning o'zida faqat bitta tizim getUpdates orqali
# "tinglashi" mumkin - agar shu tokenni boshqa tashqi tizim (masalan Idea Flow) ham
# kuzatayotgan bo'lsa, ikkalasi xabarlarni bir-biridan tortishib, ikkalasi ham
# ishonchsiz ishlab qolishi mumkin.
INBOUND_BOT_TOKEN = os.environ.get("INBOUND_BOT_TOKEN", "")


def disk_usage() -> dict:
    """STORAGE_DIR joylashgan diskning umumiy holati + biz egallagan hajm."""
    total, used, free = shutil.disk_usage(STORAGE_DIR)
    our_usage = 0
    for d in (VIDEOS_DIR, CHUNKS_DIR, RESULTS_DIR, TTS_DIR, UPLOADS_DIR):
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    our_usage += p.stat().st_size
                except OSError:
                    pass
    return {
        "disk_total": total,
        "disk_used": used,
        "disk_free": free,
        "app_usage": our_usage,
        "storage_limit": STORAGE_LIMIT,
    }


def has_space_for(extra_bytes: int) -> bool:
    usage = disk_usage()
    if usage["app_usage"] + extra_bytes > STORAGE_LIMIT:
        return False
    if extra_bytes > usage["disk_free"] - (1024 * 1024 * 1024):  # 1GB xavfsizlik zaxirasi
        return False
    return True


def safe_name(name: str) -> str:
    """Path traversal himoyasi - faqat fayl nomini oladi, kataloglarni olib tashlaydi."""
    name = Path(name or "file").name
    name = name.replace("..", "_")
    return name or "file"
