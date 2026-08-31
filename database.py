"""
SQLite orqali persistent holat. Kichik shaxsiy loyiha uchun yetarli -
har bir so'rov qisqa muddatli bo'lib, global lock bilan himoyalanadi.
"""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from storage import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL;")
_conn.execute("PRAGMA foreign_keys=ON;")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def new_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def tx():
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def init_db():
    with tx() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                original_name TEXT,
                filename TEXT,
                path TEXT,
                file_size INTEGER DEFAULT 0,
                duration REAL DEFAULT 0,
                status TEXT DEFAULT 'uploading',
                chunk_count INTEGER DEFAULT 0,
                language TEXT DEFAULT '',
                instruction TEXT DEFAULT '',
                detected_language TEXT DEFAULT '',
                progress REAL DEFAULT 0,
                message TEXT DEFAULT '',
                error TEXT,
                repetition_chunk_index INTEGER,
                repetition_info TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                video_id TEXT,
                chunk_index INTEGER,
                start_time REAL,
                end_time REAL,
                path TEXT,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                error TEXT,
                transcript TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT,
                ts TEXT,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS results (
                id TEXT PRIMARY KEY,
                video_id TEXT,
                kind TEXT,
                filename TEXT,
                path TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY,
                original_name TEXT,
                total_size INTEGER,
                received_size INTEGER DEFAULT 0,
                tmp_path TEXT,
                status TEXT DEFAULT 'uploading',
                video_id TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                label TEXT,
                key_encrypted TEXT,
                masked TEXT,
                active INTEGER DEFAULT 1,
                status TEXT DEFAULT 'unknown',
                last_checked_at TEXT,
                last_error TEXT,
                last_used_at TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tts_jobs (
                id TEXT PRIMARY KEY,
                title TEXT,
                provider TEXT,
                voice TEXT,
                mood TEXT,
                speed REAL DEFAULT 1.0,
                instructions TEXT,
                aisha_key_encrypted TEXT,
                stretch_to_fit INTEGER DEFAULT 1,
                status TEXT DEFAULT 'queued',
                total_segments INTEGER DEFAULT 0,
                completed_segments INTEGER DEFAULT 0,
                result_path TEXT,
                error TEXT,
                created_at TEXT,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tts_segments (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                seg_index INTEGER,
                start_sec REAL,
                end_sec REAL,
                text TEXT,
                status TEXT DEFAULT 'pending',
                audio_path TEXT,
                cache_key TEXT,
                attempts INTEGER DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS costs (
                id TEXT PRIMARY KEY,
                video_id TEXT,
                kind TEXT,
                amount_usd REAL DEFAULT 0,
                detail TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                name TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS cloud_files (
                id TEXT PRIMARY KEY,
                kind TEXT DEFAULT 'video',
                original_name TEXT,
                filename TEXT,
                path TEXT,
                file_size INTEGER DEFAULT 0,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS translation_memory_chat (
                id TEXT PRIMARY KEY,
                role TEXT,
                content TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS translation_memory_notes (
                id TEXT PRIMARY KEY,
                content TEXT,
                source_message_id TEXT,
                created_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_video ON chunks(video_id);
            CREATE INDEX IF NOT EXISTS idx_logs_video ON job_logs(video_id);
            CREATE INDEX IF NOT EXISTS idx_results_video ON results(video_id);
            CREATE INDEX IF NOT EXISTS idx_ttsseg_job ON tts_segments(job_id);
            CREATE INDEX IF NOT EXISTS idx_costs_video ON costs(video_id);
            """
        )
    _migrate_columns()


# ---------------------------------------------------------------------------
# Yengil migratsiya: mavjud bazaga yangi ustunlarni qo'shadi (agar yo'q bo'lsa)
# ---------------------------------------------------------------------------

_VIDEO_NEW_COLUMNS = {
    "thumbnail_path": "TEXT",
    "blocked_reason": "TEXT",
    "transcript_text": "TEXT",
    "transcript_segments": "TEXT",
    "transcript_approved": "INTEGER DEFAULT 0",
    "translation_text": "TEXT",
    "translation_segments": "TEXT",
    "translation_status": "TEXT DEFAULT 'none'",
    "translation_source": "TEXT",
    "audio_path": "TEXT",
    "audio_status": "TEXT DEFAULT 'none'",
    "tts_job_id": "TEXT",
    "final_video_path": "TEXT",
    "final_video_status": "TEXT DEFAULT 'none'",
    "cost_total": "REAL DEFAULT 0",
    "started_at": "TEXT",
    "flagged_issues": "TEXT",
    "topic_group": "TEXT",
    "freeze_points": "TEXT",
    "folder_id": "TEXT",
    "idea_flow_sent_at": "TEXT",
    "telegram_send_status": "TEXT DEFAULT 'none'",
    "telegram_send_error": "TEXT",
    "kind": "TEXT DEFAULT 'pipeline'",
    "split_total_parts": "INTEGER DEFAULT 0",
    "split_parts_sent": "INTEGER DEFAULT 0",
}
_UPLOAD_NEW_COLUMNS = {
    "kind": "TEXT DEFAULT 'pipeline'",
    "file_kind": "TEXT DEFAULT 'video'",
}
_TTS_JOB_NEW_COLUMNS = {
    "video_id": "TEXT",
    "freeze_points": "TEXT",
}
_API_KEY_NEW_COLUMNS = {
    "provider": "TEXT DEFAULT 'openai'",
}


def _migrate_columns():
    with tx() as c:
        existing = {row[1] for row in c.execute("PRAGMA table_info(videos)").fetchall()}
        for col, decl in _VIDEO_NEW_COLUMNS.items():
            if col not in existing:
                c.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl}")
        existing_tts = {row[1] for row in c.execute("PRAGMA table_info(tts_jobs)").fetchall()}
        for col, decl in _TTS_JOB_NEW_COLUMNS.items():
            if col not in existing_tts:
                c.execute(f"ALTER TABLE tts_jobs ADD COLUMN {col} {decl}")
        existing_keys = {row[1] for row in c.execute("PRAGMA table_info(api_keys)").fetchall()}
        for col, decl in _API_KEY_NEW_COLUMNS.items():
            if col not in existing_keys:
                c.execute(f"ALTER TABLE api_keys ADD COLUMN {col} {decl}")
        existing_uploads = {row[1] for row in c.execute("PRAGMA table_info(uploads)").fetchall()}
        for col, decl in _UPLOAD_NEW_COLUMNS.items():
            if col not in existing_uploads:
                c.execute(f"ALTER TABLE uploads ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------------------
# Umumiy helperlar
# ---------------------------------------------------------------------------

def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def fetchone(sql, params=()):
    with tx() as c:
        cur = c.execute(sql, params)
        return row_to_dict(cur.fetchone())


def fetchall(sql, params=()):
    with tx() as c:
        cur = c.execute(sql, params)
        return [row_to_dict(r) for r in cur.fetchall()]


def execute(sql, params=()):
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def log_line(video_id: str, message: str):
    execute(
        "INSERT INTO job_logs (video_id, ts, message) VALUES (?, ?, ?)",
        (video_id, now(), message),
    )


def get_logs(video_id: str, limit: int = 300):
    return fetchall(
        "SELECT ts, message FROM job_logs WHERE video_id = ? ORDER BY id DESC LIMIT ?",
        (video_id, limit),
    )[::-1]


def add_cost(video_id: str, kind: str, amount_usd: float, detail: str = ""):
    execute(
        "INSERT INTO costs (id, video_id, kind, amount_usd, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (new_id(), video_id, kind, amount_usd, detail, now()),
    )
    if video_id:
        execute("UPDATE videos SET cost_total = COALESCE(cost_total, 0) + ? WHERE id = ?", (amount_usd, video_id))


def get_setting(key: str, default=None):
    r = fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return r["value"] if r else default


def set_setting(key: str, value: str):
    execute("INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
