"""Idea Flow (Lovable/Supabase) ma'lumotlarini Tarjima bazasidagi idea_* jadvallarga ko'chiradi.

Lovable Cloud -> Database'dan har bir jadvalni CSV (yoki JSON) qilib yuklab oling va
bitta papkaga soling. Fayl nomi jadval nomi bilan boshlanishi kerak, masalan:
profiles.csv yoki profiles_rows.csv, folders_rows.csv, items_rows.csv, tasks_rows.csv,
ideas_rows.csv, inbox_items_rows.csv, attachments_rows.csv, comments_rows.csv,
reminders_rows.csv, activity_log_rows.csv, allowed_telegram_users_rows.csv,
bot_states_rows.csv. Topilmagan jadvallar o'tkazib yuboriladi.

Serverda (bot hali yoqilmasdan, IDEA_BOT_TOKEN qo'yilishidan OLDIN) ishga tushiring:

    sudo STORAGE_DIR=/opt/tarjima-storage /opt/tarjima/.venv/bin/python \\
        /opt/tarjima/scripts/import_ideaflow.py /papka/yo'li

Bazada Idea Flow ma'lumoti allaqachon bo'lsa, skript to'xtaydi; eskisini o'chirib
qaytadan import qilish uchun --replace qo'shing.
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db  # noqa: E402

csv.field_size_limit(sys.maxsize)

TABLES = ["profiles", "folders", "items", "tasks", "ideas", "inbox_items", "attachments", "comments",
          "reminders", "activity_log", "allowed_telegram_users", "bot_states"]
TIMESTAMPS = {"created_at", "updated_at", "completed_at", "remind_at", "sent_at"}
BOOLEANS = {"daily_review_enabled", "is_admin"}
INTEGERS = {"telegram_user_id", "telegram_chat_id", "chat_id", "telegram_message_id", "telegram_update_id",
            "sort_order", "file_size", "repeat_every_minutes", "repeat_remaining"}
JSON_COLUMNS = {"raw", "ai_suggestion", "state"}


def to_utc(value: str) -> str:
    """Postgres/ISO vaqt -> db.now() formati (UTC, 'YYYY-MM-DDTHH:MM:SS')."""
    v = value.strip().replace(" ", "T", 1)
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    elif len(v) > 3 and v[-3] in "+-" and v[-2:].isdigit():
        v += ":00"  # '+00' -> '+00:00'
    dt = datetime.fromisoformat(v)
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def convert(col: str, value):
    if value is None or value == "":
        return None
    if col in JSON_COLUMNS:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if col in TIMESTAMPS:
        return to_utc(str(value))
    if col in BOOLEANS:
        return 1 if str(value).strip().lower() in ("true", "t", "1", "yes") else 0
    if col in INTEGERS:
        return int(float(value))
    if col == "daily_review_time":
        return str(value)[:5]
    return value


def find_file(folder: Path, table: str):
    for path in sorted(folder.iterdir()):
        stem = path.stem.lower()
        if path.suffix.lower() in (".csv", ".json") and (
                stem == table or stem.startswith(table + "_rows") or stem.startswith(table + "-")):
            return path
    return None


def read_rows(path: Path) -> list:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("rows", [])
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def table_columns(table: str) -> set:
    return {r["name"] for r in db.fetchall(f"PRAGMA table_info({table})")}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="Eksport qilingan CSV/JSON fayllar papkasi")
    parser.add_argument("--replace", action="store_true", help="Mavjud Idea Flow ma'lumotini o'chirib qayta yozish")
    args = parser.parse_args()
    if not args.folder.is_dir():
        sys.exit(f"Papka topilmadi: {args.folder}")

    db.init_db()
    existing = db.fetchone("SELECT COUNT(*) AS c FROM idea_profiles")["c"]
    if existing and not args.replace:
        sys.exit("Bazada Idea Flow ma'lumoti allaqachon bor. Qayta import qilish uchun --replace qo'shing.")
    if args.replace:
        for table in TABLES:
            db.execute(f"DELETE FROM idea_{table}")

    total = 0
    for table in TABLES:
        path = find_file(args.folder, table)
        if not path:
            print(f"  - {table}: fayl topilmadi, o'tkazib yuborildi")
            continue
        target = f"idea_{table}"
        allowed = table_columns(target)
        rows = read_rows(path)
        count = 0
        for row in rows:
            values = {col: convert(col, val) for col, val in row.items() if col in allowed}
            if table == "folders" and values.get("name") is None:
                values["name"] = "Nomsiz"
            if table == "items" and values.get("title") is None:
                values["title"] = "Nomsiz"
            if not values:
                continue
            db.execute(f"INSERT OR REPLACE INTO {target} ({', '.join(values)}) "
                       f"VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
            count += 1
        total += count
        print(f"  ✓ {table}: {count} ta qator ({path.name})")

    owner = db.fetchone("SELECT telegram_user_id, telegram_username FROM idea_profiles "
                        "WHERE telegram_user_id IS NOT NULL ORDER BY is_admin DESC, created_at LIMIT 1")
    print(f"\nJami {total} ta qator import qilindi.")
    if owner:
        print(f"Ma'lumot egasi: Telegram ID {owner['telegram_user_id']} (@{owner['telegram_username'] or '-'})")
    else:
        print("DIQQAT: Telegram bog'langan profil topilmadi - botga birinchi /start bosgan odam egasi bo'ladi.")


if __name__ == "__main__":
    main()
