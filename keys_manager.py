"""
API kalitlarini xavfsiz boshqarish (OpenAI va Claude/Anthropic).

- Kalitlar diskda Fernet (symmetric) shifrlash bilan saqlanadi.
- Frontendga hech qachon to'liq kalit qaytarilmaydi, faqat maskalangan ko'rinish.
- Har bir provider (openai/claude) uchun bir nechta aktiv kalit orasida
  rotatsiya qilinadi (rate limit/auth xatosida keyingisiga o'tadi).
"""
import os

from cryptography.fernet import Fernet, InvalidToken

import database as db
from storage import SECRET_KEY_PATH


def _load_or_create_fernet() -> Fernet:
    env_secret = os.environ.get("APP_SECRET", "").strip()
    if env_secret:
        # APP_SECRET dan 32 baytli kalit hosil qilamiz
        import base64
        import hashlib
        key = base64.urlsafe_b64encode(hashlib.sha256(env_secret.encode()).digest())
        return Fernet(key)
    if SECRET_KEY_PATH.exists():
        key = SECRET_KEY_PATH.read_bytes()
    else:
        key = Fernet.generate_key()
        SECRET_KEY_PATH.write_bytes(key)
    return Fernet(key)


_fernet = _load_or_create_fernet()


def mask_key(raw: str) -> str:
    raw = raw.strip()
    if len(raw) <= 8:
        return "sk-...."
    return f"{raw[:3]}...{raw[-4:]}"


def add_key(raw_key: str, label: str = "", provider: str = "openai") -> dict:
    raw_key = raw_key.strip()
    if not raw_key:
        raise ValueError("API kalit bo'sh bo'lishi mumkin emas.")
    enc = _fernet.encrypt(raw_key.encode()).decode()
    kid = db.new_id()
    db.execute(
        """INSERT INTO api_keys (id, label, key_encrypted, masked, active, status, created_at, provider)
           VALUES (?, ?, ?, ?, 1, 'unknown', ?, ?)""",
        (kid, label or mask_key(raw_key), enc, mask_key(raw_key), db.now(), provider),
    )
    return get_key_public(kid)


def _decrypt(enc: str) -> str:
    try:
        return _fernet.decrypt(enc.encode()).decode()
    except InvalidToken:
        return ""


def list_keys_public(provider: str = None) -> list:
    if provider:
        rows = db.fetchall("SELECT * FROM api_keys WHERE provider = ? ORDER BY created_at ASC", (provider,))
    else:
        rows = db.fetchall("SELECT * FROM api_keys ORDER BY created_at ASC")
    return [_public(r) for r in rows]


def _public(r: dict) -> dict:
    return {
        "id": r["id"],
        "label": r["label"],
        "masked": r["masked"],
        "active": bool(r["active"]),
        "status": r["status"],
        "provider": r["provider"] or "openai",
        "last_checked_at": r["last_checked_at"],
        "last_error": r["last_error"],
        "created_at": r["created_at"],
    }


def get_key_public(kid: str) -> dict:
    r = db.fetchone("SELECT * FROM api_keys WHERE id = ?", (kid,))
    return _public(r) if r else None


def delete_key(kid: str):
    db.execute("DELETE FROM api_keys WHERE id = ?", (kid,))


def set_active(kid: str, active: bool):
    db.execute("UPDATE api_keys SET active = ? WHERE id = ?", (1 if active else 0, kid))


def mark_result(kid: str, ok: bool, error: str = None):
    db.execute(
        "UPDATE api_keys SET status = ?, last_checked_at = ?, last_error = ?, last_used_at = ? WHERE id = ?",
        ("ok" if ok else "error", db.now(), (error or "")[:500], db.now(), kid),
    )


def encrypt_raw(raw: str) -> str:
    return _fernet.encrypt(raw.encode()).decode()


def decrypt_raw(enc: str) -> str:
    return _decrypt(enc)


def raw_key_for(kid: str) -> str:
    r = db.fetchone("SELECT key_encrypted FROM api_keys WHERE id = ?", (kid,))
    if not r:
        return ""
    return _decrypt(r["key_encrypted"])


def get_next_active_key(exclude_ids=None, provider: str = "openai"):
    """Navbatdagi ishlatiladigan aktiv kalitni tanlaydi (eng kam ishlatilgan / xatosiz)."""
    exclude_ids = exclude_ids or set()
    rows = db.fetchall(
        "SELECT * FROM api_keys WHERE active = 1 AND provider = ? ORDER BY "
        "(CASE WHEN status = 'error' THEN 1 ELSE 0 END), "
        "(last_used_at IS NULL) DESC, last_used_at ASC",
        (provider,),
    )
    for r in rows:
        if r["id"] not in exclude_ids:
            raw = _decrypt(r["key_encrypted"])
            if raw:
                return r["id"], raw
    return None, None


def has_any_active_key(provider: str = "openai") -> bool:
    r = db.fetchone("SELECT COUNT(*) as c FROM api_keys WHERE active = 1 AND provider = ?", (provider,))
    return bool(r and r["c"] > 0)


async def test_key(kid: str) -> dict:
    import httpx
    r = db.fetchone("SELECT provider FROM api_keys WHERE id = ?", (kid,))
    provider = (r["provider"] if r else None) or "openai"
    raw = raw_key_for(kid)
    if not raw:
        return {"ok": False, "error": "Kalit topilmadi."}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if provider == "claude":
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": raw, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                          "messages": [{"role": "user", "content": "hi"}]},
                )
            else:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {raw}"},
                )
        if resp.status_code < 300:
            mark_result(kid, True)
            return {"ok": True}
        else:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            mark_result(kid, False, err)
            return {"ok": False, "error": err}
    except Exception as e:
        mark_result(kid, False, str(e))
        return {"ok": False, "error": str(e)}
