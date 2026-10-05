"""Idea Flow: sun'iy intellektsiz, oddiy qoidalarga asoslangan o'zbekcha tahlil.

Lovable versiyasidagi src/lib/nlp.server.ts'ning aynan ko'chirmasi. Vaqtlar
"mahalliy devor soati" sifatida naive datetime bilan ifodalanadi (now_in_tz);
UTC'ga o'girish uchun offset ayiriladi.
"""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Tartib muhim: "shanba" boshqa kunlar nomining ichida ham bor.
WEEKDAYS = [
    ("yakshanba", 6),
    ("dushanba", 0),
    ("seshanba", 1),
    ("chorshanba", 2),
    ("payshanba", 3),
    ("juma", 4),
    ("shanba", 5),
]

_IDEA_RE = re.compile(r"^(idea|ideya|g'oya|goya)\s*[:\-]", re.I)
_IDEA_PREFIX_RE = re.compile(r"^(idea|ideya|g'oya|goya)\s*[:\-]\s*", re.I)
_A = re.ASCII


def tz_offset_minutes(tz: str) -> int:
    try:
        offset = datetime.now(ZoneInfo(tz)).utcoffset()
        return round(offset.total_seconds() / 60)
    except Exception:
        return 300


def now_in_tz(tz: str) -> datetime:
    """Shu vaqt zonasidagi hozirgi devor soati (naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=tz_offset_minutes(tz))


def to_iso_date(d) -> str:
    return d.strftime("%Y-%m-%d")


def utc_str(dt: datetime) -> str:
    """Naive UTC datetime -> db.now() formati."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def local_to_utc(local: datetime, offset_minutes: int) -> datetime:
    return local - timedelta(minutes=offset_minutes)


def _next_weekday(base: datetime, wd: int) -> datetime:
    diff = (wd - base.weekday() + 7) % 7 or 7
    return base + timedelta(days=diff)


def parse_message(raw: str, now: datetime, offset_minutes: int) -> dict:
    """Natija: {kind: task|idea|reminder|note, title, due_date, remind_at (UTC, db formati)}."""
    text = raw.strip()
    lower = text.lower()

    if _IDEA_RE.search(text):
        return {"kind": "idea", "title": _IDEA_PREFIX_RE.sub("", text, count=1).strip() or text,
                "due_date": None, "remind_at": None}

    day = None
    matched = ""
    if re.search(r"\bbugun\b", lower, _A):
        day, matched = now, "bugun"
    elif re.search(r"\bertaga\b", lower, _A):
        day, matched = now + timedelta(days=1), "ertaga"
    elif re.search(r"\bindinga\b", lower, _A):
        day, matched = now + timedelta(days=2), "indinga"
    else:
        for name, wd in WEEKDAYS:
            if name in lower:
                day, matched = _next_weekday(now, wd), name
                break

    time_match = (re.search(r"\b(\d{1,2})[:.](\d{2})\b", lower, _A)
                  or re.search(r"\bsoat\s+(\d{1,2})\b", lower, _A))
    hours = None
    minutes = 0
    if time_match:
        hours = int(time_match.group(1))
        minutes = int(time_match.group(2)) if time_match.lastindex and time_match.lastindex >= 2 else 0
        if hours > 23:
            hours = None

    wants_reminder = re.search(r"\beslat", lower, _A) is not None

    title = text
    if matched:
        title = re.sub(re.escape(matched), "", title, count=1, flags=re.I).strip()
    title = re.sub(r"\bsoat\s*\d{1,2}\b", "", title, count=1, flags=re.I | _A)
    title = re.sub(r"\bsoat\b", "", title, count=1, flags=re.I | _A)
    title = re.sub(r"\b\d{1,2}[:.]\d{2}\b", "", title, count=1, flags=_A)
    title = re.sub(r"\beslat\w*", "", title, count=1, flags=re.I | _A)
    title = re.sub(r"(^|\s)(da|dagi|ga)(\s|$)", " ", title, count=1, flags=re.I)
    title = re.sub(r"\s{2,}", " ", title)
    title = re.sub(r"^[\s,\-–:]+|[\s,\-–:]+$", "", title).strip()
    if not title:
        title = text

    if wants_reminder and (day is not None or hours is not None):
        d = (day or now).replace(hour=hours if hours is not None else 9, minute=minutes, second=0, microsecond=0)
        if day is None and d <= now:
            d += timedelta(days=1)
        return {"kind": "reminder", "title": title, "due_date": to_iso_date(d),
                "remind_at": utc_str(local_to_utc(d, offset_minutes))}

    if day is not None:
        return {"kind": "task", "title": title, "due_date": to_iso_date(day), "remind_at": None}

    return {"kind": "note", "title": text, "due_date": None, "remind_at": None}


def parse_reminder_input(text: str, tz: str):
    """'2026-08-30 09:00 har 2 soat 3 marta' -> {remind_at, repeat_every, repeat_remaining} yoki None."""
    dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    tm = re.search(r"(\d{1,2}):(\d{2})", text)
    if not dm or not tm:
        return None
    try:
        local = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)), int(tm.group(1)), int(tm.group(2)))
    except ValueError:
        return None
    remind_at = utc_str(local_to_utc(local, tz_offset_minutes(tz)))

    repeat_every = None
    rep = re.search(r"har\s+(\d{1,3})\s*(soat|daqiqa|daq|minut)", text, re.I)
    if rep:
        repeat_every = int(rep.group(1)) * 60 if re.search("soat", rep.group(2), re.I) else int(rep.group(1))
    repeat_count = None
    cnt = re.search(r"(\d{1,3})\s*marta", text, re.I)
    if cnt:
        repeat_count = int(cnt.group(1))
    return {
        "remind_at": remind_at,
        "repeat_every": repeat_every,
        "repeat_remaining": max((repeat_count or 1) - 1, 0) if repeat_every else None,
    }


def local_date_time(utc_iso: str, tz: str) -> str:
    """UTC (db formati) -> 'YYYY-MM-DD HH:MM' shu vaqt zonasida."""
    dt = datetime.fromisoformat(utc_iso) + timedelta(minutes=tz_offset_minutes(tz))
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_date(iso_date, today_iso: str) -> str:
    if not iso_date:
        return "sanasiz"
    if iso_date == today_iso:
        return "bugun"
    if iso_date == to_iso_date(date.fromisoformat(today_iso) + timedelta(days=1)):
        return "ertaga"
    return iso_date
