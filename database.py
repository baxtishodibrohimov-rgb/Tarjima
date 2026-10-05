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

            -- Asosiy (birinchi yaratilgan) audio/video har doim videos jadvalining
            -- o'zidagi eski ustunlarda (tts_job_id/audio_path/final_video_path/...)
            -- saqlanadi - ESKI VIDEOLAR VA MAVJUD KOD SHU BILAN ISHLASHDA DAVOM ETADI.
            -- Bu jadval FAQAT video 'completed' bo'lgach, IKKINCHI provayder bilan
            -- QO'SHIMCHA yaratilgan audio/video uchun (masalan asosiysi Aisha bilan
            -- qilingan bo'lsa, shu yerda OpenAI varianti saqlanadi, yoki aksincha).
            CREATE TABLE IF NOT EXISTS audio_tracks (
                id TEXT PRIMARY KEY,
                video_id TEXT,
                provider TEXT,
                tts_job_id TEXT,
                audio_path TEXT,
                audio_status TEXT DEFAULT 'none',
                final_video_path TEXT,
                final_video_status TEXT DEFAULT 'none',
                freeze_points TEXT,
                error TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            -- "Ruscha o'rganish" rejimi: foydalanuvchi qo'lda yuklagan tayyor
            -- Learning SRT asosida, MAVJUD TTS va render mexanizmi orqali
            -- yaratiladigan MUSTAQIL audio/video. Bitta video uchun bitta
            -- Learning holat (video_id UNIQUE) - asosiy videos.* va
            -- audio_tracks jadvaliga UMUMAN tegmaydi. Dastur bu yerga hech
            -- qanday SRT YARATMAYDI - srt_path faqat foydalanuvchi yuklagan
            -- faylni ko'rsatadi, o'zgartirilmasdan saqlanadi.
            CREATE TABLE IF NOT EXISTS learning_tracks (
                id TEXT PRIMARY KEY,
                video_id TEXT,
                srt_filename TEXT,
                srt_path TEXT,
                srt_status TEXT DEFAULT 'none',
                segment_count INTEGER DEFAULT 0,
                provider TEXT,
                voice TEXT,
                mood TEXT,
                speed REAL DEFAULT 1.0,
                instructions TEXT,
                stretch_to_fit INTEGER DEFAULT 1,
                tts_job_id TEXT,
                audio_path TEXT,
                audio_status TEXT DEFAULT 'none',
                freeze_points TEXT,
                final_video_path TEXT,
                final_video_status TEXT DEFAULT 'none',
                error TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- Freeze-point ustuvorlik zanjirining ENG OXIRGI, zaxira chorasi -
            -- TTS tezligi (tag/avtomatik, 0.85-1.20) VA audio birlashtirishdagi
            -- qayta namunalash (shu chegara ichida) IKKALASI HAM yetmagan
            -- (kam uchraydigan) holatlarda yoziladi - qanchalik tez-tez
            -- ishlatilayotganini kuzatish uchun (agar ko'p bo'lsa, 0.85-1.20
            -- chegarasi qayta ko'rib chiqilishi kerak degani).
            CREATE TABLE IF NOT EXISTS freeze_point_events (
                id TEXT PRIMARY KEY,
                video_id TEXT,
                tts_job_id TEXT,
                seg_index INTEGER,
                source_time REAL,
                duration REAL,
                applied_speed REAL,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                name TEXT,
                parent_id TEXT,
                sort_order INTEGER DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT DEFAULT '',
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                quota_bytes INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                updated_at TEXT,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT,
                expires_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                PRIMARY KEY(user_id, key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_video ON chunks(video_id);
            CREATE INDEX IF NOT EXISTS idx_logs_video ON job_logs(video_id);
            CREATE INDEX IF NOT EXISTS idx_results_video ON results(video_id);
            CREATE INDEX IF NOT EXISTS idx_ttsseg_job ON tts_segments(job_id);
            CREATE INDEX IF NOT EXISTS idx_costs_video ON costs(video_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_audio_tracks_video_provider ON audio_tracks(video_id, provider);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_tracks_video ON learning_tracks(video_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

            -- Idea Flow (Telegram shaxsiy produktivlik boti, ideaflow_bot.py).
            -- Lovable/Supabase'dan ko'chirilgan; Tarjima'ning o'z jadvallari bilan
            -- (masalan folders) to'qnashmasligi uchun idea_ prefiksi bilan.
            -- Vaqtlar db.now() formatida (UTC, 'YYYY-MM-DDTHH:MM:SS').
            CREATE TABLE IF NOT EXISTS idea_profiles (
                id TEXT PRIMARY KEY,
                email TEXT,
                full_name TEXT,
                telegram_user_id INTEGER UNIQUE,
                telegram_chat_id INTEGER,
                telegram_username TEXT,
                timezone TEXT NOT NULL DEFAULT 'Asia/Tashkent',
                daily_review_time TEXT NOT NULL DEFAULT '19:00',
                daily_review_enabled INTEGER NOT NULL DEFAULT 1,
                last_daily_review_on TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_folders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                parent_folder_id TEXT,
                root_type TEXT NOT NULL DEFAULT 'base',
                name TEXT NOT NULL,
                description TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_items (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                folder_id TEXT,
                root_type TEXT NOT NULL DEFAULT 'base',
                type TEXT NOT NULL DEFAULT 'note',
                title TEXT NOT NULL DEFAULT 'Nomsiz',
                content TEXT,
                url TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                due_date TEXT,
                status TEXT NOT NULL DEFAULT 'todo',
                priority TEXT NOT NULL DEFAULT 'normal',
                idea_id TEXT,
                completed_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_ideas (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                planned_date TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_inbox_items (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'text',
                text TEXT,
                url TEXT,
                telegram_message_id INTEGER,
                telegram_update_id INTEGER,
                raw TEXT,
                ai_suggestion TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_attachments (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                related_type TEXT NOT NULL,
                related_id TEXT NOT NULL,
                file_kind TEXT NOT NULL DEFAULT 'file',
                file_name TEXT,
                mime_type TEXT,
                file_size INTEGER,
                telegram_file_id TEXT,
                storage_path TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_comments (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                related_type TEXT NOT NULL,
                related_id TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_reminders (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                related_type TEXT NOT NULL,
                related_id TEXT,
                title TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                recurrence TEXT,
                sent_at TEXT,
                repeat_every_minutes INTEGER,
                repeat_remaining INTEGER,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_activity_log (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                related_type TEXT NOT NULL,
                related_id TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_allowed_telegram_users (
                id TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                label TEXT,
                added_by TEXT,
                chat_id INTEGER,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS idea_bot_states (
                user_id TEXT PRIMARY KEY,
                state TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_idea_items_folder ON idea_items(user_id, root_type, folder_id);
            CREATE INDEX IF NOT EXISTS idx_idea_folders_parent ON idea_folders(user_id, root_type, parent_folder_id);
            CREATE INDEX IF NOT EXISTS idx_idea_tasks_user ON idea_tasks(user_id, status, due_date);
            CREATE INDEX IF NOT EXISTS idx_idea_reminders_due ON idea_reminders(status, remind_at);
            CREATE INDEX IF NOT EXISTS idx_idea_attachments_rel ON idea_attachments(related_type, related_id);
            """
        )
    _migrate_columns()


# ---------------------------------------------------------------------------
# Yengil migratsiya: mavjud bazaga yangi ustunlarni qo'shadi (agar yo'q bo'lsa)
# ---------------------------------------------------------------------------

_VIDEO_NEW_COLUMNS = {
    "owner_id": "TEXT",
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
    "cost_total_som": "REAL DEFAULT 0",
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
    # "Video bo'lish": none / splitting / ready / error - qismlar SPLIT_DIR'da
    # "Botga jo'natish" bosilguncha saqlanadi.
    "split_status": "TEXT DEFAULT 'none'",
    "split_error": "TEXT",
    # Saytdagi "Bot" bo'limiga (Telegram botdagi Video Baza) yuklash holati.
    "bot_upload_status": "TEXT DEFAULT 'none'",
    "bot_upload_error": "TEXT",
    "bot_upload_progress": "TEXT",
    "bot_upload_folder_id": "TEXT",
    "bot_upload_title": "TEXT",
    "bot_item_id": "TEXT",
    "split_restore_target": "TEXT",
    # Asosiy yakuniy videoga subtitr "kuydirilgan" (hardsub) nusxasi - ixtiyoriy,
    # foydalanuvchi so'rasa yaratiladi, asosiy final_video_path'ga UMUMAN tegmaydi.
    # Xatosi ham ALOHIDA ustunda (umumiy `error` maydonini "band" qilib
    # qo'ymaslik uchun - u asosiy quvur xatolari uchun ishlatiladi).
    "subtitled_video_status": "TEXT DEFAULT 'none'",
    "subtitled_video_path": "TEXT",
    "subtitled_video_error": "TEXT",
}
_UPLOAD_NEW_COLUMNS = {
    "owner_id": "TEXT",
    "kind": "TEXT DEFAULT 'pipeline'",
    "file_kind": "TEXT DEFAULT 'video'",
}
_TTS_JOB_NEW_COLUMNS = {
    "owner_id": "TEXT",
    "video_id": "TEXT",
    "freeze_points": "TEXT",
    # 1 = bu ish asosiy (primary) audio EMAS, balki video 'completed' bo'lgach
    # ikkinchi provayder bilan QO'SHIMCHA yaratilgan track uchun - shuning uchun
    # tugagach videos.* (asosiy) maydonlarga tegilmaydi, faqat audio_tracks
    # jadvali yangilanadi.
    "for_track": "INTEGER DEFAULT 0",
    # 1 = bu for_track ish 'oddiy qo'shimcha provayder treki' EMAS, balki
    # Learning treki uchun - sync_video_from_tts_job() shu belgi bilan
    # ikkalasini bir-biridan ajratadi (audio_tracks vs learning_tracks).
    "is_learning": "INTEGER DEFAULT 0",
}
_API_KEY_NEW_COLUMNS = {
    "owner_id": "TEXT",
    "provider": "TEXT DEFAULT 'openai'",
}
_CLOUD_FILE_NEW_COLUMNS = {
    "owner_id": "TEXT",
    "thumbnail_path": "TEXT",
}
_FOLDER_NEW_COLUMNS = {
    "owner_id": "TEXT",
    "parent_id": "TEXT",
    "sort_order": "INTEGER DEFAULT 0",
}
_COSTS_NEW_COLUMNS = {
    "owner_id": "TEXT",
    # Aisha TTS narxi so'mda beriladi (dollarga aylantirilmaydi - kurs
    # o'zgarib turishi mumkin, shuning uchun o'z valyutasida saqlanadi).
    # amount_usd ustuni bo'sh (0) qoladi - shunda umumiy $ summasi (cost_total)
    # buzilmaydi.
    "amount_som": "REAL DEFAULT 0",
}
_AUDIO_TRACK_NEW_COLUMNS = {
    # Shu (ikkinchi provayder) trekning yakuniy videosiga subtitr "kuydirilgan"
    # (hardsub) nusxasi - ixtiyoriy, final_video_path'ga tegmaydi. Xatosi
    # ALOHIDA ustunda - trekning umumiy `error`sini (audio/render xatosi
    # uchun ishlatiladi) "bosib qo'ymasligi" uchun.
    "subtitled_video_status": "TEXT DEFAULT 'none'",
    "subtitled_video_path": "TEXT",
    "subtitled_video_error": "TEXT",
}
_TTS_SEGMENT_NEW_COLUMNS = {
    # Tashqi tayyorlangan SRT (parse_srt_direct) timestamp qatoridan o'qilgan
    # ixtiyoriy [speed:fast]/[speed:slow] belgisi - "fast"/"slow"/NULL (oddiy).
    "speed_tag": "TEXT",
    # YAKUNIY (freeze-point ham hisobga olingan) holatda audio blok vaqt
    # oralig'iga sig'MADIMI - 1 = freeze-point haqiqatan ishga tushdi (ya'ni
    # TTS tezligi + qayta namunalash 0.85-1.20 byudjeti ikkalasi ham
    # yetmadi), 0 = sig'di. merge_job()da, freeze-point aniq ishga tushgan
    # paytda belgilanadi - shuning uchun aniq (soxta signal bermaydi).
    "duration_overflow": "INTEGER DEFAULT 0",
    # TTS'ga so'ralgan yakuniy "speed" qiymati (tag/avtomatik moslashuvdan
    # keyin, 0.85-1.20 chegarasida) - audio birlashtirish bosqichi shuni
    # bilib, qolgan "joy"ni hisoblab qayta namunalaydi (ikkala bosqich
    # BIRGALIKDA hech qachon 1.20dan oshmasligi uchun).
    "applied_speed": "REAL",
}
_CHUNK_NEW_COLUMNS = {
    # NULL = bo'lak uchun alohida til belgilanmagan (videoning umumiy tilidan foydalaniladi),
    # "" = bu bo'lak uchun ataylab "avtomatik aniqlash" tanlangan, "xx" = aniq til kodi.
    "language": "TEXT",
    # Foydalanuvchi "Bo'lakni qayta yubor" tugmasini bosganda 1 ga o'rnatiladi - shu bo'lak
    # bir martalik, kichik (30-60s) qismlarga bo'lib qayta transkripsiya qilinishini bildiradi.
    "force_split": "INTEGER DEFAULT 0",
}

_MEMORY_NEW_COLUMNS = {"owner_id": "TEXT"}
_USER_NEW_COLUMNS = {"display_name": "TEXT DEFAULT ''"}
_LEARNING_TRACK_NEW_COLUMNS = {
    # Learning SRT vaqt qatoridagi [yangi:..]/[takror:..] teglari (translation.parse_learning_srt)
    "words_json": "TEXT",
    "warnings_json": "TEXT",
    # So'zlar kadrga kuydirilgan (ixtiyoriy intro bilan) yuklab olinadigan Learning videosi.
    # final_video_path (intro'siz, toza) o'zgarmaydi - pleyer shuni ishlatadi.
    "export_status": "TEXT DEFAULT 'none'",
    "export_video_path": "TEXT",
    "export_with_intro": "INTEGER DEFAULT 0",
    "export_error": "TEXT",
    "intro_status": "TEXT DEFAULT 'none'",
    "intro_progress": "REAL DEFAULT 0",
    "intro_message": "TEXT",
    "intro_error": "TEXT",
    "intro_duration": "REAL DEFAULT 0",
    "intro_video_path": "TEXT",
    "intro_slides_json": "TEXT",
    "intro_strip_stress": "INTEGER DEFAULT 0",
    "intro_aisha_key_encrypted": "TEXT",
    # Learning videosi uchun ham asosiy Matn -> Video oqimidagi kabi alohida
    # hardsub nusxa yaratiladi. Toza/export video o'zgartirilmaydi.
    "subtitled_video_status": "TEXT DEFAULT 'none'",
    "subtitled_video_path": "TEXT",
    "subtitled_video_error": "TEXT",
}


def _migrate_columns():
    with tx() as c:
        existing_users = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        for col, decl in _USER_NEW_COLUMNS.items():
            if col not in existing_users:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
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
        existing_cloud = {row[1] for row in c.execute("PRAGMA table_info(cloud_files)").fetchall()}
        for col, decl in _CLOUD_FILE_NEW_COLUMNS.items():
            if col not in existing_cloud:
                c.execute(f"ALTER TABLE cloud_files ADD COLUMN {col} {decl}")
        existing_folders = {row[1] for row in c.execute("PRAGMA table_info(folders)").fetchall()}
        for col, decl in _FOLDER_NEW_COLUMNS.items():
            if col not in existing_folders:
                c.execute(f"ALTER TABLE folders ADD COLUMN {col} {decl}")
        existing_chunks = {row[1] for row in c.execute("PRAGMA table_info(chunks)").fetchall()}
        for col, decl in _CHUNK_NEW_COLUMNS.items():
            if col not in existing_chunks:
                c.execute(f"ALTER TABLE chunks ADD COLUMN {col} {decl}")
        existing_costs = {row[1] for row in c.execute("PRAGMA table_info(costs)").fetchall()}
        for col, decl in _COSTS_NEW_COLUMNS.items():
            if col not in existing_costs:
                c.execute(f"ALTER TABLE costs ADD COLUMN {col} {decl}")
        existing_tracks = {row[1] for row in c.execute("PRAGMA table_info(audio_tracks)").fetchall()}
        for col, decl in _AUDIO_TRACK_NEW_COLUMNS.items():
            if col not in existing_tracks:
                c.execute(f"ALTER TABLE audio_tracks ADD COLUMN {col} {decl}")
        existing_tts_segments = {row[1] for row in c.execute("PRAGMA table_info(tts_segments)").fetchall()}
        for col, decl in _TTS_SEGMENT_NEW_COLUMNS.items():
            if col not in existing_tts_segments:
                c.execute(f"ALTER TABLE tts_segments ADD COLUMN {col} {decl}")
        for table in ("translation_memory_chat", "translation_memory_notes"):
            existing_memory = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, decl in _MEMORY_NEW_COLUMNS.items():
                if col not in existing_memory:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        existing_learning = {row[1] for row in c.execute("PRAGMA table_info(learning_tracks)").fetchall()}
        for col, decl in _LEARNING_TRACK_NEW_COLUMNS.items():
            if col not in existing_learning:
                c.execute(f"ALTER TABLE learning_tracks ADD COLUMN {col} {decl}")
        for table in ("videos", "uploads", "api_keys", "tts_jobs", "costs", "folders", "cloud_files",
                      "translation_memory_chat", "translation_memory_notes"):
            c.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_owner ON {table}(owner_id)")


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


def add_cost(video_id: str, kind: str, amount_usd: float, detail: str = "", amount_som: float = 0,
             owner_id: str = None):
    """amount_usd - AQSH dollarida (OpenAI, Claude va h.k.). amount_som - o'zbek
    so'mida (masalan Aisha TTS) - ikkalasi turli valyuta, shuning uchun
    ARALASHTIRILMAYDI: har biri o'z ustunida (va videoning o'z cost_total/
    cost_total_som ustunida) alohida yig'iladi."""
    if not owner_id and video_id:
        video = fetchone("SELECT owner_id FROM videos WHERE id = ?", (video_id,))
        owner_id = video["owner_id"] if video else None
    execute(
        "INSERT INTO costs (id, video_id, kind, amount_usd, amount_som, detail, created_at, owner_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), video_id, kind, amount_usd, amount_som, detail, now(), owner_id),
    )
    if video_id:
        execute("UPDATE videos SET cost_total = COALESCE(cost_total, 0) + ?, "
                "cost_total_som = COALESCE(cost_total_som, 0) + ? WHERE id = ?",
                (amount_usd, amount_som, video_id))


def get_setting(key: str, default=None):
    r = fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return r["value"] if r else default


def set_setting(key: str, value: str):
    execute("INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def get_user_setting(user_id: str, key: str, default=None):
    r = fetchone("SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (user_id, key))
    return r["value"] if r else default


def set_user_setting(user_id: str, key: str, value: str):
    execute("INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value", (user_id, key, value))
