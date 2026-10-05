"""O'chirilgan videolardan qolib ketgan "yetim" fayl va yozuvlarni topadi.

Oldingi versiyalarda video o'chirilganda ikkinchi provayder (Aisha/OpenAI)
audiolari va eski TTS ishlari diskda qolib ketardi. Bu skript bazada egasi
yo'q papkalarni topib, hajmini ko'rsatadi; --apply bilan o'chiradi.
Xarajatlar (costs) tarixiga tegmaydi.

Ishlatish (serverda, xizmat to'xtatilgan holda):
    sudo systemctl stop tarjima
    sudo STORAGE_DIR=/opt/tarjima-storage python3 /opt/tarjima/scripts/cleanup_orphans.py          # faqat ko'rsatadi
    sudo STORAGE_DIR=/opt/tarjima-storage python3 /opt/tarjima/scripts/cleanup_orphans.py --apply  # o'chiradi
    sudo systemctl start tarjima
"""
import os
import shutil
import sys
from pathlib import Path

if not os.environ.get("STORAGE_DIR"):
    sys.exit("STORAGE_DIR berilmagan (masalan: STORAGE_DIR=/opt/tarjima-storage).")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db  # noqa: E402
from storage import (CHUNKS_DIR, CLOUD_DIR, DB_PATH, RESULTS_DIR, SPLIT_DIR, TTS_DIR, UPLOADS_DIR,  # noqa: E402
                     VIDEOS_DIR)

VIDEO_TABLES = ("chunks", "results", "job_logs", "audio_tracks", "learning_tracks", "freeze_point_events")


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def gb(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GB"


def main(apply: bool):
    if not DB_PATH.exists():
        sys.exit(f"Baza topilmadi: {DB_PATH} - STORAGE_DIR to'g'rimi?")
    db.init_db()
    videos = {r["id"] for r in db.fetchall("SELECT id FROM videos")}
    clouds = {r["id"] for r in db.fetchall("SELECT id FROM cloud_files")}
    video_dirs = [p for p in VIDEOS_DIR.iterdir() if p.is_dir()]
    if not videos and len(video_dirs) > 3:
        sys.exit("Bazada birorta video yo'q, lekin diskda papkalar bor - STORAGE_DIR noto'g'ri bo'lishi mumkin. "
                 "Hech narsa o'chirilmadi.")

    # Egasi o'chirilgan TTS ishlari (videosiz, alohida TTS ishlari saqlanadi)
    orphan_jobs = {r["id"] for r in db.fetchall(
        "SELECT id FROM tts_jobs WHERE video_id IS NOT NULL AND video_id NOT IN (SELECT id FROM videos)")}
    for table in ("audio_tracks", "learning_tracks"):
        orphan_jobs |= {r["tts_job_id"] for r in db.fetchall(
            f"SELECT tts_job_id FROM {table} WHERE tts_job_id IS NOT NULL AND video_id NOT IN (SELECT id FROM videos)")}
    jobs = {r["id"] for r in db.fetchall("SELECT id FROM tts_jobs")} - orphan_jobs

    paths = []
    for base in (VIDEOS_DIR, CHUNKS_DIR, RESULTS_DIR):
        paths += [p for p in base.iterdir() if p.is_dir() and p.name not in videos]
    paths += [p for p in SPLIT_DIR.iterdir() if p.is_dir() and p.name.removeprefix("bot_") not in videos]
    paths += [p for p in TTS_DIR.iterdir() if p.is_dir() and p.name != "cache" and p.name not in jobs]
    paths += [p for p in CLOUD_DIR.iterdir() if p.is_dir() and p.name not in clouds]
    active_uploads = {Path(r["tmp_path"]).name for r in db.fetchall(
        "SELECT tmp_path FROM uploads WHERE status = 'uploading' AND tmp_path IS NOT NULL")}
    paths += [p for p in UPLOADS_DIR.glob("*.part") if p.name not in active_uploads]

    rows = {t: db.fetchone(f"SELECT COUNT(*) n FROM {t} WHERE video_id NOT IN (SELECT id FROM videos)")["n"]
            for t in VIDEO_TABLES}

    total = 0
    for p in sorted(paths):
        n = size_of(p)
        total += n
        print(f"{gb(n):>10}  {p}")
    print(f"\nJami diskda: {gb(total)} ({len(paths)} ta papka/fayl)")
    stale = ", ".join(f"{t}={n}" for t, n in rows.items() if n) or "yo'q"
    print(f"Yetim TTS ishlari: {len(orphan_jobs)}; bazadagi yetim yozuvlar: {stale}")

    if not apply:
        print("\nHech narsa o'chirilmadi. O'chirish uchun --apply bilan qayta ishga tushiring.")
        return
    for p in paths:
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    for job_id in orphan_jobs:
        db.execute("DELETE FROM tts_segments WHERE job_id = ?", (job_id,))
        db.execute("DELETE FROM tts_jobs WHERE id = ?", (job_id,))
    for t in VIDEO_TABLES:
        db.execute(f"DELETE FROM {t} WHERE video_id NOT IN (SELECT id FROM videos)")
    print(f"\nO'chirildi: {gb(total)} bo'shadi.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
