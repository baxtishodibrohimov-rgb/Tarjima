import asyncio
import itertools
from datetime import datetime, timedelta, timezone

import pytest

import database as db
import ideaflow_bot as bot
import ideaflow_nlp as nlp

OWNER = 111
STRANGER = 222
TABLES = ["profiles", "folders", "items", "tasks", "ideas", "inbox_items", "attachments", "comments",
          "reminders", "activity_log", "allowed_telegram_users", "bot_states"]


@pytest.fixture()
def calls(monkeypatch):
    db.init_db()
    for table in TABLES:
        db.execute(f"DELETE FROM idea_{table}")
    recorded = []
    message_ids = itertools.count(1000)

    async def fake_tg(method, body=None):
        recorded.append((method, {k: v for k, v in (body or {}).items() if v is not None}))
        return {"message_id": next(message_ids)} if method.startswith("send") else True

    monkeypatch.setattr(bot, "tg", fake_tg)
    return recorded


_ids = itertools.count(1)


def message(text=None, frm=OWNER, **extra):
    msg = {"message_id": next(_ids), "chat": {"id": frm}, "from": {"id": frm, "username": f"u{frm}"}}
    if text is not None:
        msg["text"] = text
    msg.update(extra)
    return {"update_id": next(_ids), "message": msg}


def callback(data, frm=OWNER):
    return {"update_id": next(_ids), "callback_query": {
        "id": "cb", "data": data, "from": {"id": frm}, "message": {"message_id": 5, "chat": {"id": frm}}}}


def send(*updates):
    async def run():
        for u in updates:
            await bot._handle_update(u)
    asyncio.run(run())


def sent_texts(calls):
    return [b.get("text", "") for m, b in calls if m == "sendMessage"]


def test_natural_language_creates_task_reminder_and_idea(calls):
    send(message("/start"), message("ertaga mijozga qo'ng'iroq"), message("juma 15:00 da eslat: hisobot"),
         message("idea: yangi rubrika"))
    owner = db.fetchone("SELECT * FROM idea_profiles")
    assert owner["telegram_user_id"] == OWNER and owner["is_admin"] == 1
    tomorrow = nlp.to_iso_date(nlp.now_in_tz("Asia/Tashkent") + timedelta(days=1))
    assert db.fetchone("SELECT title, due_date FROM idea_tasks") == {"title": "mijozga qo'ng'iroq", "due_date": tomorrow}
    reminder = db.fetchone("SELECT title, remind_at FROM idea_reminders")
    assert reminder["title"] == "hisobot" and reminder["remind_at"].endswith("T10:00:00")
    assert db.fetchone("SELECT title FROM idea_ideas")["title"] == "yangi rubrika"


def test_capture_media_then_file_into_video_base(calls):
    send(message("/start"), message(None, video={"file_id": "VID", "file_name": "dars.mp4"}))
    inbox = db.fetchone("SELECT * FROM idea_inbox_items")
    assert any("Qayerga joylaymiz" in t for t in sent_texts(calls))
    send(callback(f"k:v:{inbox['id']}"))
    item = db.fetchone("SELECT * FROM idea_items")
    attachment = db.fetchone("SELECT * FROM idea_attachments")
    assert item["root_type"] == "video_base" and item["type"] == "video" and item["title"] == "dars.mp4"
    assert attachment["related_type"] == "item" and attachment["related_id"] == item["id"]


def test_browse_folder_saves_into_current_folder(calls):
    send(message("/start"), message("📚 Baza"), message("➕ Papka"), message("Marketing"),
         message("📁 Marketing"), message("Reels ro'yxati"))
    folder = db.fetchone("SELECT * FROM idea_folders")
    item = db.fetchone("SELECT * FROM idea_items")
    assert folder["name"] == "Marketing" and item["folder_id"] == folder["id"] and item["root_type"] == "base"


def test_stranger_rejected_until_admin_allows(calls):
    send(message("/start"), message("salom", frm=STRANGER))
    assert any("Kirish huquqi yo'q" in t for t in sent_texts(calls))
    calls.clear()
    send(callback("u:add:0"), message(str(STRANGER)), message("/bugun", frm=STRANGER))
    assert not any("Kirish huquqi" in t for t in sent_texts(calls))
    assert bot.chats_for_owner(db.fetchone("SELECT id FROM idea_profiles")["id"]) == [OWNER, STRANGER]


def test_due_repeating_reminder_is_sent_and_rescheduled(calls):
    send(message("/start"))
    owner = db.fetchone("SELECT id FROM idea_profiles")["id"]
    past = nlp.utc_str(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1))
    db.execute("INSERT INTO idea_reminders (id, user_id, related_type, title, remind_at, status, "
               "repeat_every_minutes, repeat_remaining) VALUES ('r1', ?, 'note', 'Suv ich', ?, 'pending', 120, 2)",
               (owner, past))
    calls.clear()
    assert asyncio.run(bot.run_due_reminders()) == 1
    assert any("Suv ich" in t for t in sent_texts(calls))
    row = db.fetchone("SELECT * FROM idea_reminders WHERE id = 'r1'")
    assert row["status"] == "pending" and row["repeat_remaining"] == 1 and row["remind_at"] > db.now()


def test_tarjima_video_appears_in_bot(calls):
    assert bot.add_tarjima_video("Ma'ruza", "http://x/final-download") is False  # bot hali ulanmagan
    send(message("/start"))
    assert bot.add_tarjima_video("Ma'ruza", "http://x/final-download") is True
    calls.clear()
    send(message("🌐 Tarjima"))
    assert any("Ma'ruza" in t for t in sent_texts(calls))
