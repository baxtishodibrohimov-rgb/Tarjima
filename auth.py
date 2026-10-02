"""Cookie-session authentication and tenant ownership helpers."""
import contextvars
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

import database as db

SESSION_COOKIE = "darslik_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
TOTAL_CAPACITY = int(os.environ.get("TOTAL_STORAGE_LIMIT", str(180 * 1024**3)))
ADMIN_QUOTA = int(os.environ.get("ADMIN_STORAGE_LIMIT", str(95 * 1024**3)))
USER_QUOTA = int(os.environ.get("USER_STORAGE_LIMIT", str(10 * 1024**3)))
MAX_REGULAR_USERS = int(os.environ.get("MAX_REGULAR_USERS", "5"))

_current_user = contextvars.ContextVar("current_user", default=None)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    rounds = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        ).hex()
        return hmac.compare_digest(actual, digest_hex)
    except (TypeError, ValueError):
        return False


def public_user(user: dict) -> dict:
    return {
        "id": user["id"], "username": user["username"], "role": user["role"],
        "is_superadmin": user["role"] == "superadmin",
        "quota_bytes": int(user["quota_bytes"] or 0), "active": bool(user["active"]),
        "created_at": user.get("created_at"), "last_login_at": user.get("last_login_at"),
    }


def bootstrap_superadmin(username: str, password: str) -> dict:
    admin = db.fetchone("SELECT * FROM users WHERE role = 'superadmin' ORDER BY created_at LIMIT 1")
    if not admin:
        if not username or not password:
            raise RuntimeError("APP_USERNAME va APP_PASSWORD super-admin yaratish uchun majburiy.")
        admin_id = db.new_id()
        db.execute(
            "INSERT INTO users (id, username, password_hash, role, quota_bytes, active, created_at, updated_at) "
            "VALUES (?, ?, ?, 'superadmin', ?, 1, ?, ?)",
            (admin_id, username.strip(), hash_password(password), ADMIN_QUOTA, db.now(), db.now()),
        )
        admin = db.fetchone("SELECT * FROM users WHERE id = ?", (admin_id,))

    # Old single-user data belongs to the first super-admin.
    for table in ("videos", "uploads", "api_keys", "tts_jobs", "costs", "folders", "cloud_files",
                  "translation_memory_chat", "translation_memory_notes"):
        db.execute(f"UPDATE {table} SET owner_id = ? WHERE owner_id IS NULL OR owner_id = ''", (admin["id"],))
    for setting in db.fetchall("SELECT key, value FROM settings"):
        if db.get_user_setting(admin["id"], setting["key"]) is None:
            db.set_user_setting(admin["id"], setting["key"], setting["value"])
    return admin


def authenticate(username: str, password: str):
    user = db.fetchone("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),))
    if not user or not user["active"] or not verify_password(password, user["password_hash"]):
        return None
    db.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (db.now(), db.now(), user["id"]))
    return db.fetchone("SELECT * FROM users WHERE id = ?", (user["id"],))


def create_session(user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_DAYS)
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))
    db.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
               (token_hash, user_id, now.isoformat(), expires.isoformat()))
    return token, expires.isoformat()


def user_from_request(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return db.fetchone(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
        (token_hash, datetime.now(timezone.utc).isoformat()),
    )


def delete_session(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))


def set_current_user(user):
    return _current_user.set(user)


def reset_current_user(token):
    _current_user.reset(token)


def current_user(required: bool = True):
    user = _current_user.get()
    if required and not user:
        raise HTTPException(401, "Avval tizimga kiring.")
    return user


def current_user_id(required: bool = True):
    user = current_user(required)
    return user["id"] if user else None


def require_superadmin():
    user = current_user()
    if user["role"] != "superadmin":
        raise HTTPException(403, "Bu amal faqat super-admin uchun.")
    return user


def user_usage(user_id: str) -> int:
    # Source files are the quota boundary. Derived chunks/results can be rebuilt and
    # are intentionally not double-counted against the same uploaded video.
    video = db.fetchone("SELECT COALESCE(SUM(file_size), 0) n FROM videos WHERE owner_id = ?", (user_id,))
    cloud = db.fetchone("SELECT COALESCE(SUM(file_size), 0) n FROM cloud_files WHERE owner_id = ?", (user_id,))
    pending = db.fetchone(
        "SELECT COALESCE(SUM(total_size), 0) n FROM uploads WHERE owner_id = ? AND status = 'uploading'", (user_id,))
    return int(video["n"] or 0) + int(cloud["n"] or 0) + int(pending["n"] or 0)


def has_user_space(extra_bytes: int, user=None) -> bool:
    user = user or current_user()
    return user_usage(user["id"]) + int(extra_bytes) <= int(user["quota_bytes"] or 0)


def allocated_capacity(exclude_user_id: str = None) -> int:
    if exclude_user_id:
        r = db.fetchone("SELECT COALESCE(SUM(quota_bytes), 0) n FROM users WHERE active = 1 AND id != ?",
                        (exclude_user_id,))
    else:
        r = db.fetchone("SELECT COALESCE(SUM(quota_bytes), 0) n FROM users WHERE active = 1")
    return int(r["n"] or 0)


def active_regular_user_count(exclude_user_id: str = None) -> int:
    """Return the number of active non-admin accounts sharing the storage pool."""
    if exclude_user_id:
        row = db.fetchone(
            "SELECT COUNT(*) n FROM users WHERE role = 'user' AND active = 1 AND id != ?",
            (exclude_user_id,),
        )
    else:
        row = db.fetchone("SELECT COUNT(*) n FROM users WHERE role = 'user' AND active = 1")
    return int(row["n"] or 0)


def _owned(table: str, object_id: str, owner_id: str, id_column: str = "id") -> bool:
    if table == "results":
        return bool(db.fetchone(
            "SELECT 1 ok FROM results r JOIN videos v ON v.id = r.video_id "
            "WHERE r.id = ? AND v.owner_id = ?", (object_id, owner_id)))
    return bool(db.fetchone(f"SELECT 1 ok FROM {table} WHERE {id_column} = ? AND owner_id = ?", (object_id, owner_id)))


def authorize_resource(request: Request, user: dict):
    """Reject cross-tenant object IDs before an endpoint can touch them."""
    path = request.url.path
    if user["role"] == "superadmin":
        return

    admin_prefixes = ("/api/admin", "/api/debug", "/api/split-videos", "/api/cloud-files")
    if path.startswith(admin_prefixes) or path.endswith("/send-to-bot"):
        raise HTTPException(403, "Bu bo'lim faqat super-admin uchun.")

    checks = [
        (r"^/api/videos/(?!upload(?:/|$))([^/]+)", "videos", "id"),
        (r"^/api/jobs/([^/]+)", "videos", "id"),
        (r"^/api/results/([^/]+)", "results", "id"),
        (r"^/api/tts/jobs/([^/]+)", "tts_jobs", "id"),
        (r"^/api/api-keys/([^/]+)", "api_keys", "id"),
        (r"^/api/folders/([^/]+)", "folders", "id"),
    ]
    for pattern, table, column in checks:
        match = re.match(pattern, path)
        if match and not _owned(table, match.group(1), user["id"], column):
            raise HTTPException(404, "Ma'lumot topilmadi.")
