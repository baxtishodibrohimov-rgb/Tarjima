"""Idea Flow - shaxsiy Telegram produktivlik va bilim bazasi boti.

Lovable'dagi Idea Flow ilovasining (src/lib/bot.server.ts) Python ko'chirmasi:
xabar/media darhol saqlanadi, keyin toifa tanlanadi (Vazifa / Ideya / Baza /
Video Baza / Keyinroq); tabiiy til ("ertaga ...", "juma 15:00 da eslat ...",
"idea: ..."); cheksiz ichma-ich papkali Baza va Video Baza; saytga video yuklash;
eslatmalar va kunlik hisobot; ruxsat berilgan foydalanuvchilar.

Lovable'dan farqi: webhook o'rniga long polling (ochiq HTTPS manzil kerak
emas), Supabase o'rniga Tarjima'ning SQLite bazasi (idea_* jadvallar), cron
o'rniga har daqiqada ishlaydigan fon tsikli.
"""
import asyncio
import json
import re
import traceback
from datetime import date, datetime, timedelta, timezone

import httpx

import database as db
import ideaflow_nlp as nlp
import telegram_bot
from storage import IDEA_BOT_API_URL, IDEA_BOT_TOKEN

POLL_TIMEOUT = 30
LAST_UPDATE_ID_KEY = "ideaflow_bot_last_update_id"
BOT_USERNAME_KEY = "ideaflow_bot_username"

MAIN_KEYBOARD = [
    ["📅 Bugun", "💡 Ideyalar"],
    ["📚 Baza", "🎬 Video Baza"],
    ["☁️ Webga yuklash", "⏰ Eslatmalar"],
    ["⚙️ Sozlamalar"],
]
# "🌐 Tarjima" - eski klaviatura tugmasi (Telegram ilovasida yangi klaviatura
# kelguncha ko'rinib turadi), u ham "☁️ Webga yuklash"ni ochadi.
WEB_UPLOAD_TEXTS = ("☁️ Webga yuklash", "🌐 Tarjima")
MENU_TEXTS = ["📅 Bugun", "💡 Ideyalar", "📚 Baza", "🎬 Video Baza", *WEB_UPLOAD_TEXTS, "⏰ Eslatmalar",
              "⚙️ Sozlamalar"]

HELP = """<b>Shaxsiy yordamchi bot</b>

Istalgan narsani yozing yoki yuboring — darhol saqlanadi, keyin toifasini tanlaysiz.

Misollar:
• <code>ertaga mijozga qo'ng'iroq</code> — ertangi vazifa
• <code>juma 15:00 da eslat: hisobot</code> — eslatma
• <code>idea: yangi reels rubrikasi</code> — ideya
• Rasm, video, ovoz, hujjat, havola — Bazaga saqlanadi

Buyruqlar:
/bugun — bugungi vazifalar
/ideyalar — ideyalar
/baza — bilim bazasi
/yuklash — saytga (Bulutga) video yuklash
/eslatmalar — yaqin eslatmalar
/qidir so'z — qidiruv
/sozlamalar — vaqt zonasi va papkalar"""

REMINDER_PROMPT = """⏰ Sana va vaqtni yozing.

Misollar:
<code>2026-08-30 09:00</code>
<code>2026-08-30 09:00 har 2 soat 3 marta</code>

(takrorlanish ixtiyoriy)"""

_UNSET = object()
_background = set()
_client = None


def _spawn(coro):
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _now_utc() -> str:
    return db.now()


def _utc_after(minutes: int) -> str:
    return nlp.utc_str(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=minutes))


# ------------------------------------------------------------------ telegram

def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=60)
    return _client


async def tg(method: str, body: dict = None):
    payload = {k: v for k, v in (body or {}).items() if v is not None}
    resp = await _http().post(f"{IDEA_BOT_API_URL.rstrip('/')}/bot{IDEA_BOT_TOKEN}/{method}", json=payload)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed [{resp.status_code}]: {resp.text[:300]}")
    return data.get("result")


def escape_html(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def send_message(chat_id, text, keyboard=None, reply_keyboard=None):
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        body["reply_markup"] = {"inline_keyboard": keyboard}
    elif reply_keyboard:
        body["reply_markup"] = {"keyboard": reply_keyboard, "resize_keyboard": True}
    return await tg("sendMessage", body)


async def send_media(chat_id, kind, file_id, caption=None, keyboard=None):
    method, field = {
        "video": ("sendVideo", "video"),
        "image": ("sendPhoto", "photo"),
        "audio": ("sendAudio", "audio"),
    }.get(kind, ("sendDocument", "document"))
    try:
        return await tg(method, {
            "chat_id": chat_id, field: file_id, "caption": caption, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": keyboard} if keyboard else None,
        })
    except Exception as e:
        print(f"[ideaflow_bot] {method}: {e}", flush=True)
        return None


async def edit_message_text(chat_id, message_id, text, keyboard=None):
    try:
        await tg("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard} if keyboard else None,
        })
    except Exception as e:
        print(f"[ideaflow_bot] editMessageText: {e}", flush=True)


async def edit_reply_markup(chat_id, message_id, keyboard=None):
    try:
        await tg("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": keyboard} if keyboard else None,
        })
    except Exception as e:
        print(f"[ideaflow_bot] editMessageReplyMarkup: {e}", flush=True)


async def answer_callback(callback_id, text=None):
    try:
        await tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except Exception as e:
        print(f"[ideaflow_bot] answerCallbackQuery: {e}", flush=True)


async def delete_message(chat_id, message_id):
    try:
        await tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


async def flash(chat_id, text, ms=4000):
    """Xabarni ko'rsatib, so'ng chatni toza qoldirish uchun o'chiradi (bloklamaydi)."""
    res = await send_message(chat_id, text)
    if res and res.get("message_id"):
        async def later():
            await asyncio.sleep(ms / 1000)
            await delete_message(chat_id, res["message_id"])
        _spawn(later())


# ------------------------------------------------------------------ access

def owner_profile():
    """Ma'lumot egasi - barcha ulangan akkauntlar shu profil ma'lumotini ko'radi."""
    return db.fetchone("SELECT * FROM idea_profiles WHERE telegram_user_id IS NOT NULL "
                       "ORDER BY is_admin DESC, created_at ASC LIMIT 1")


def _create_profile_for(frm, chat_id, is_admin):
    tg_id = frm.get("id")
    free = db.fetchone("SELECT id FROM idea_profiles WHERE telegram_user_id IS NULL ORDER BY created_at LIMIT 1")
    if free:
        user_id = free["id"]
    else:
        user_id = db.new_id()
        db.execute("INSERT INTO idea_profiles (id, email, full_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                   (user_id, f"tg{tg_id}@telegram.local", frm.get("first_name") or "Telegram", db.now(), db.now()))
    db.execute("UPDATE idea_profiles SET telegram_user_id = ?, telegram_chat_id = ?, telegram_username = ?, "
               "is_admin = ?, updated_at = ? WHERE id = ?",
               (tg_id, chat_id, frm.get("username"), 1 if is_admin else 0, db.now(), user_id))
    return db.fetchone("SELECT * FROM idea_profiles WHERE id = ?", (user_id,))


def chats_for_owner(owner_id):
    """Egaga tegishli barcha chatlar (asosiy + ruxsat berilgan akkauntlar)."""
    chats = []
    owner = db.fetchone("SELECT telegram_chat_id FROM idea_profiles WHERE id = ?", (owner_id,))
    if owner and owner["telegram_chat_id"]:
        chats.append(int(owner["telegram_chat_id"]))
    for a in db.fetchall("SELECT chat_id FROM idea_allowed_telegram_users"):
        if a["chat_id"] and int(a["chat_id"]) not in chats:
            chats.append(int(a["chat_id"]))
    return chats


def resolve_profile(frm, chat_id):
    tg_id = int((frm or {}).get("id") or 0)
    owner = owner_profile()
    # birinchi ulangan odam - admin va ma'lumot egasi
    if not owner:
        return _create_profile_for(frm or {}, chat_id, True)

    if int(owner["telegram_user_id"]) == tg_id:
        if owner["telegram_chat_id"] != chat_id:
            db.execute("UPDATE idea_profiles SET telegram_chat_id = ? WHERE id = ?", (chat_id, owner["id"]))
        return {**owner, "telegram_chat_id": chat_id}

    allowed = db.fetchone("SELECT id, chat_id FROM idea_allowed_telegram_users WHERE telegram_user_id = ?", (tg_id,))
    if not allowed:
        return None
    if allowed["chat_id"] != chat_id:
        db.execute("UPDATE idea_allowed_telegram_users SET chat_id = ? WHERE id = ?", (chat_id, allowed["id"]))
    # ruxsat berilgan akkaunt ham xuddi shu ma'lumot bilan ishlaydi
    return {**owner, "telegram_chat_id": chat_id, "is_admin": 1}


# ------------------------------------------------------------------ state

def get_row(user_id):
    """Saqlangan qator: joriy dialog holati (s) + navigatsiya joyi (nav) alohida."""
    r = db.fetchone("SELECT state FROM idea_bot_states WHERE user_id = ?", (user_id,))
    raw = json.loads(r["state"]) if r and r["state"] else None
    if not raw:
        return None, None
    if raw.get("t"):  # eski format: to'g'ridan-to'g'ri holat
        nav = {"root": raw.get("root"), "folder": raw.get("folder")} if raw["t"] == "browse" else None
        return raw, nav
    return raw.get("s"), raw.get("nav")


def get_state(user_id):
    return get_row(user_id)[0]


def set_state(user_id, state, nav=_UNSET):
    _, cur_nav = get_row(user_id)
    if nav is not _UNSET:
        next_nav = nav
    elif state and state.get("t") == "browse":
        next_nav = {"root": state["root"], "folder": state["folder"]}
    else:
        next_nav = cur_nav
    db.execute("INSERT INTO idea_bot_states (user_id, state, updated_at) VALUES (?, ?, ?) "
               "ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
               (user_id, json.dumps({"s": state, "nav": next_nav}), db.now()))


# ------------------------------------------------------------------ helpers

def _insert(table, **cols):
    cols.setdefault("id", db.new_id())
    db.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
               tuple(cols.values()))
    return cols["id"]


def _now_cols():
    now = db.now()
    return {"created_at": now, "updated_at": now}


def log(user_id, action, related_type, related_id, detail=None):
    _insert("idea_activity_log", user_id=user_id, action=action, related_type=related_type,
            related_id=related_id, detail=detail, created_at=db.now())


def _today(p):
    return nlp.to_iso_date(nlp.now_in_tz(p["timezone"]))


def capture_keyboard(inbox_id):
    return [
        [{"text": "✅ Vazifa", "callback_data": f"k:t:{inbox_id}"},
         {"text": "💡 Ideya", "callback_data": f"k:i:{inbox_id}"}],
        [{"text": "📚 Baza", "callback_data": f"k:b:{inbox_id}"},
         {"text": "🎬 Video Baza", "callback_data": f"k:v:{inbox_id}"}],
        [{"text": "📥 Keyinroq", "callback_data": f"k:l:{inbox_id}"}],
    ]


def task_keyboard(task_id):
    return [
        [{"text": "✅ Bajarildi", "callback_data": f"t:d:{task_id}"},
         {"text": "❌ Bajarilmadi", "callback_data": f"t:n:{task_id}"}],
        [{"text": "⏰ Eslatish", "callback_data": f"t:r:{task_id}"},
         {"text": "🗑 O'chirish", "callback_data": f"t:x:{task_id}"}],
    ]


def item_groups(item_id):
    """Elementning fayl guruhlari [(tartib, nom, [qismlar...])]: loyihada asl video,
    o'zbekcha video, SRT, audio va h.k. Guruhsiz (eski) elementda - bitta guruh."""
    rows = db.fetchall("SELECT file_kind, telegram_file_id, file_name, group_label, "
                       "COALESCE(group_order, 0) AS group_order FROM idea_attachments "
                       "WHERE related_type = 'item' AND related_id = ? AND telegram_file_id IS NOT NULL "
                       "ORDER BY group_order, rowid", (item_id,))
    groups = {}
    for r in rows:
        groups.setdefault(r["group_order"], {"label": r["group_label"], "parts": []})["parts"].append(r)
    return [(order, g["label"], g["parts"]) for order, g in sorted(groups.items())]


def item_keyboard(item_id):
    # Asosiy (birinchi) guruhdan tashqari har bir fayl uchun tugma - bosilsa alohida keladi.
    extra = [{"text": label or f"Fayl {order}", "callback_data": f"tg:{item_id}:{order}"}
             for order, label, _ in item_groups(item_id)[1:]]
    rows = [extra[i:i + 2] for i in range(0, len(extra), 2)]
    return rows + [
        [{"text": "✏️ Tahrirlash", "callback_data": f"tv:e:{item_id}"}],
        [{"text": "🗑 O'chirish", "callback_data": f"tv:x:{item_id}"}],
    ]


def item_edit_keyboard(item_id):
    return [
        [{"text": "📍 Joylashuvni o'zgartirish", "callback_data": f"tv:m:{item_id}"}],
        [{"text": "⬅️ Ortga", "callback_data": f"tv:b:{item_id}"}],
    ]


def extract_media(msg):
    if msg.get("photo"):
        return {"kind": "image", "file_id": msg["photo"][-1]["file_id"], "name": None}
    for key, kind in (("video", "video"), ("voice", "audio"), ("audio", "audio"), ("document", "file"),
                      ("video_note", "video")):
        if msg.get(key):
            name = msg[key].get("file_name") if key in ("video", "audio", "document") else None
            return {"kind": kind, "file_id": msg[key]["file_id"], "name": name}
    return {"kind": "text", "file_id": None, "name": None}


def _folder_children_sql(root_type, parent):
    if parent:
        return "AND parent_folder_id = ?", (root_type, parent)
    return "AND parent_folder_id IS NULL", (root_type,)


def child_folders(user_id, root_type, parent, cols="id, name"):
    cond, params = _folder_children_sql(root_type, parent)
    return db.fetchall(f"SELECT {cols} FROM idea_folders WHERE user_id = ? AND root_type = ? {cond} "
                       f"ORDER BY sort_order, created_at", (user_id, *params))


def folder_path(user_id, folder_id):
    if not folder_id:
        return "Ildiz"
    by_id = {f["id"]: f for f in db.fetchall(
        "SELECT id, name, parent_folder_id FROM idea_folders WHERE user_id = ?", (user_id,))}
    names = []
    current = folder_id
    for _ in range(20):
        f = by_id.get(current)
        if not f:
            break
        names.insert(0, f["name"])
        current = f["parent_folder_id"]
        if not current:
            break
    return " / ".join(names) if names else "Ildiz"


def _folder_parent(user_id, folder_id):
    if not folder_id:
        return None
    r = db.fetchone("SELECT parent_folder_id FROM idea_folders WHERE id = ? AND user_id = ?", (folder_id, user_id))
    return r["parent_folder_id"] if r else None


def root_of(code):
    return "base" if code == "b" else "video_base"


def code_of(root):
    return "b" if root == "base" else "v"


# ------------------------------------------------------------------ commands

async def show_today(p, chat_id):
    today = _today(p)
    tasks = db.fetchall("SELECT * FROM idea_tasks WHERE user_id = ? AND status != 'done' "
                        "AND (due_date <= ? OR due_date IS NULL) ORDER BY due_date IS NULL, due_date LIMIT 20",
                        (p["id"], today))
    if not tasks:
        await send_message(chat_id, "🎉 Bugunga vazifa yo'q.")
        return
    await send_message(chat_id, f"<b>📅 Bugun</b> — {len(tasks)} ta vazifa")
    for t in tasks:
        await send_message(chat_id, f"• <b>{escape_html(t['title'])}</b>\n<i>{nlp.fmt_date(t['due_date'], today)}</i>",
                           task_keyboard(t["id"]))


async def show_ideas(p, chat_id):
    ideas = db.fetchall("SELECT * FROM idea_ideas WHERE user_id = ? ORDER BY created_at DESC LIMIT 15", (p["id"],))
    if not ideas:
        await send_message(chat_id, "💡 Hozircha ideya yo'q. <code>idea: ...</code> deb yozing.")
        return
    await send_message(chat_id, f"<b>💡 Ideyalar</b> — {len(ideas)} ta")
    for i in ideas:
        await send_message(chat_id, f"💡 <b>{escape_html(i['title'])}</b>", [[
            {"text": "✅ Vazifaga", "callback_data": f"i:t:{i['id']}"},
            {"text": "🗑 O'chirish", "callback_data": f"i:x:{i['id']}"},
        ]])


async def show_base(p, chat_id, root_type, folder_id=None):
    parent = folder_id or None
    subfolders = child_folders(p["id"], root_type, parent)
    cond = "AND folder_id = ?" if parent else "AND folder_id IS NULL"
    params = (p["id"], root_type, parent) if parent else (p["id"], root_type)
    items = db.fetchall(f"SELECT id, title, type, url FROM idea_items WHERE user_id = ? AND root_type = ? {cond} "
                        f"ORDER BY created_at DESC LIMIT 20", params)
    title = "📚 Baza" if root_type == "base" else "🎬 Video Baza"

    # Oddiy (outline) tugmalar - chat ostidagi klaviatura
    rows = [[f"📁 {f['name']}" for f in subfolders[i:i + 2]] for i in range(0, len(subfolders), 2)]
    rows.append(["➕ Papka"])
    rows.append(["⬅️ Ortga", "🏠 Asosiy menyu"] if parent else ["🏠 Asosiy menyu"])

    set_state(p["id"], {"t": "browse", "root": root_type, "folder": parent})
    empty = "" if items else "\n\n<i>Bo'sh</i>"
    await send_message(
        chat_id,
        f"<b>{title}</b>\n📍 {escape_html(folder_path(p['id'], parent))}{empty}"
        f"\n\n<i>Shu papkaga saqlash uchun matn/video/fayl yuboring.</i>",
        None, rows)

    # Har bir element alohida xabar; media bo'lsa faylning o'zi yuboriladi.
    icon = "🎬" if root_type == "video_base" else "📄"
    for i in items:
        await send_item(chat_id, i, icon)


async def send_item(chat_id, item, icon, location=None):
    """Elementni yuboradi: media bo'lsa fayl(lar)ning o'zi (1.9 GB'dan katta video
    bir necha qism bo'lib saqlangan bo'ladi - hammasi ketma-ket), aks holda matn."""
    groups = item_groups(item["id"])
    parts = groups[0][2] if groups else []  # loyihada - faqat asosiy (asl) video
    caption = f"{icon} <b>{escape_html(item['title'])}</b>" + ("\n" + escape_html(item["url"]) if item["url"] else "")
    if groups and groups[0][1] and len(groups) > 1:
        caption += f"\n{escape_html(groups[0][1])}"
    if location:
        caption += f"\n📍 {escape_html(location)}"
    keyboard = item_keyboard(item["id"])
    sent_any = False
    for n, att in enumerate(parts, start=1):
        part_caption = f"{caption}\n<i>{n}/{len(parts)}-qism</i>" if len(parts) > 1 else caption
        if await send_media(chat_id, att["file_kind"], att["telegram_file_id"], part_caption,
                            keyboard if n == 1 else None):
            sent_any = True
    if not sent_any:
        await send_message(chat_id, caption, keyboard)


async def send_item_group(p, chat_id, item_id, order):
    """Loyiha tugmasi bosilganda - o'sha faylni (bir necha qismli bo'lsa hammasini) yuboradi."""
    item = db.fetchone("SELECT title FROM idea_items WHERE id = ? AND user_id = ?", (item_id, p["id"]))
    group = next((g for g in item_groups(item_id) if str(g[0]) == str(order)), None) if item else None
    if not group:
        await send_message(chat_id, "❌ Fayl topilmadi.")
        return
    _, label, parts = group
    caption = f"<b>{escape_html(item['title'])}</b>\n{escape_html(label or '')}"
    for n, att in enumerate(parts, start=1):
        part_caption = f"{caption}\n<i>{n}/{len(parts)}-qism</i>" if len(parts) > 1 else caption
        await send_media(chat_id, att["file_kind"], att["telegram_file_id"], part_caption)


async def show_item_by_link(p, chat_id, item_id):
    """Saytdagi "Botda ochish" havolasi (/start v_<id>) - shu videoni topib yuboradi."""
    item = db.fetchone("SELECT * FROM idea_items WHERE id = ? AND user_id = ?", (item_id, p["id"]))
    if not item:
        await send_message(chat_id, "❌ Bu video topilmadi (o'chirilgan bo'lishi mumkin).", None, MAIN_KEYBOARD)
        return
    root_label = {"base": "📚 Baza", "video_base": "🎬 Video Baza", "tarjima": "🌐 Tarjima"}.get(item["root_type"], "")
    location = f"{root_label} / {folder_path(p['id'], item['folder_id'])}" if item["root_type"] != "tarjima" else root_label
    await send_item(chat_id, item, "🎬" if item["type"] == "video" else "📄", location)


def is_browse_button(text):
    return text in ("🏠 Asosiy menyu", "⬅️ Ortga", "➕ Papka") or text.startswith("📁 ")


async def handle_browse_text(p, chat_id, text):
    """Baza ichida papka tugmalari bosilganda navigatsiya (nav joyi asosida)."""
    if not is_browse_button(text):
        return False
    _, nav = get_row(p["id"])

    if text == "🏠 Asosiy menyu":
        set_state(p["id"], None, None)
        await send_message(chat_id, "🏠 Asosiy menyu", None, MAIN_KEYBOARD)
        return True
    if not nav:
        return False

    if text == "⬅️ Ortga":
        await show_base(p, chat_id, nav["root"], _folder_parent(p["id"], nav["folder"]))
        return True

    if text == "➕ Papka":
        set_state(p["id"], {"t": "folder_new", "root": nav["root"], "parent": nav["folder"], "back": "browse"})
        await send_message(chat_id, "📁 Yangi papka nomini yozing:")
        return True

    name = text[2:].strip()
    cond, params = _folder_children_sql(nav["root"], nav["folder"])
    found = db.fetchone(f"SELECT id FROM idea_folders WHERE user_id = ? AND root_type = ? {cond} AND name = ?",
                        (p["id"], *params, name))
    if found:
        await show_base(p, chat_id, nav["root"], found["id"])
    else:
        await flash(chat_id, "❌ Papka topilmadi.", 2500)
    return True


async def show_reminders(p, chat_id):
    rows = db.fetchall("SELECT * FROM idea_reminders WHERE user_id = ? AND status = 'pending' "
                       "ORDER BY remind_at LIMIT 15", (p["id"],))
    if not rows:
        await send_message(chat_id, "⏰ Yaqin eslatmalar yo'q.")
        return
    for r in rows:
        rep = (f"\n🔁 har {round(r['repeat_every_minutes'] / 60) or 1} soatda, yana {r['repeat_remaining'] or 0} marta"
               if r["repeat_every_minutes"] else "")
        await send_message(
            chat_id, f"⏰ <b>{escape_html(r['title'])}</b>\n{nlp.local_date_time(r['remind_at'], p['timezone'])}{rep}",
            [[{"text": "🗑 O'chirish", "callback_data": f"r:x:{r['id']}"}]])


def _title_search(table, user_id, q, cols="title"):
    needle = q.casefold()
    rows = db.fetchall(f"SELECT {cols} FROM {table} WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    return [r for r in rows if needle in (r["title"] or "").casefold()][:10]


async def search(p, chat_id, q):
    if not q:
        await send_message(chat_id, "Qidirish uchun: <code>/qidir so'z</code>")
        return
    tasks = _title_search("idea_tasks", p["id"], q)
    ideas = _title_search("idea_ideas", p["id"], q)
    items = _title_search("idea_items", p["id"], q, "title, root_type")
    parts = []
    if tasks:
        parts.append("<b>Vazifalar</b>\n" + "\n".join(f"• {escape_html(t['title'])}" for t in tasks))
    if ideas:
        parts.append("<b>Ideyalar</b>\n" + "\n".join(f"• {escape_html(t['title'])}" for t in ideas))
    if items:
        parts.append("<b>Baza</b>\n" + "\n".join(
            f"• {escape_html(t['title'])} <i>({'Baza' if t['root_type'] == 'base' else 'Video'})</i>" for t in items))
    await send_message(chat_id, "\n\n".join(parts) or "Hech narsa topilmadi.")


async def show_settings(p, chat_id):
    keyboard = [
        [{"text": "📚 Baza papkalari", "callback_data": "sf:b:root"}],
        [{"text": "🎬 Video Baza papkalari", "callback_data": "sf:v:root"}],
    ]
    if p["is_admin"]:
        keyboard.append([{"text": "👥 Foydalanuvchilar", "callback_data": "u:list:0"}])
    await send_message(chat_id, f"<b>⚙️ Sozlamalar</b>\n\nVaqt zonasi: <code>{p['timezone']}</code>", keyboard)


WEB_UPLOAD_INTRO = """☁️ <b>Webga video yuklash</b>

Videoni yoki zip faylni shu yerga yuboring — u saytdagi <b>Bulut</b> bo'limiga yuklanadi. U yerdan tarjimaga yoki «Video bo'lish»ga o'tkazasiz, zip ichidan kerakli fayllarni chiqarib olasiz.

• Bir nechta faylni ketma-ket yuborsa bo'ladi
• 2 GB gacha; katta video bir necha daqiqada yuklanadi
• Tugatish uchun menyudan boshqa bo'limni tanlang"""


async def show_web_upload(p, chat_id):
    """☁️ Webga yuklash - shu rejimda yuborilgan videolar saytning Bulutiga tushadi."""
    set_state(p["id"], {"t": "web_upload"}, None)
    await send_message(chat_id, WEB_UPLOAD_INTRO, None, MAIN_KEYBOARD)


ZIP_MIME_TYPES = ("application/zip", "application/x-zip-compressed", "application/x-zip")


def web_upload_video(msg):
    """Xabardagi video, video sifatida yuborilgan hujjat yoki zip fayl:
    (file_id, nom, hajm, "video"|"zip")."""
    video = msg.get("video")
    doc = msg.get("document") or {}
    kind = "video"
    if not video and doc:
        mime = (doc.get("mime_type") or "").lower()
        if mime.startswith("video/"):
            video = doc
        elif mime in ZIP_MIME_TYPES or (doc.get("file_name") or "").lower().endswith(".zip"):
            video, kind = doc, "zip"
    if not video:
        return None
    ext = ".zip" if kind == "zip" else ".mp4"
    name = video.get("file_name") or f"{kind}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}{ext}"
    return video["file_id"], name, int(video.get("file_size") or 0), kind


async def upload_to_web(chat_id, file_id, name, size, kind="video"):
    status = None
    try:
        status = await send_message(chat_id, f"⏳ <b>{escape_html(name)}</b> serverga yuklanmoqda...")
    except Exception:
        pass

    async def notify(text):
        await send_message(chat_id, "❌ " + escape_html(text))

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            saved = await telegram_bot.download_to_cloud(client, file_id, name, notify, size_hint=size,
                                                         api_base=IDEA_BOT_API_URL, token=IDEA_BOT_TOKEN, kind=kind)
        if saved:
            await send_message(chat_id, f"✅ <b>{escape_html(saved)}</b> saytga yuklandi.\n"
                                        f"Saytdagi <b>Bulut</b> bo'limida turibdi.")
    except Exception as e:
        print(f"[ideaflow_bot] Webga yuklashda xato ({name}):", flush=True)
        traceback.print_exc()
        reason = telegram_bot.hide_token(str(e), IDEA_BOT_TOKEN)[:300]
        await send_message(chat_id, f"❌ <b>{escape_html(name)}</b> yuklanmadi: {escape_html(reason)}")
    finally:
        if status and status.get("message_id"):
            await delete_message(chat_id, status["message_id"])


async def show_item_move_targets(p, chat_id, item_id, message_id, target_root="video_base", cur=None):
    """Elementni ko'chirish - papkalar ichiga kirib boriladi va "💾 Shu papkaga
    saqlash" bosilgan joyga saqlanadi."""
    keyboard = [[{"text": f"📁 {f['name']}", "callback_data": f"tvm:o:{f['id']}"}]
                for f in child_folders(p["id"], target_root, cur)]
    keyboard.append([{"text": "💾 Shu papkaga saqlash", "callback_data": "tvm:s:0"}])
    if cur:
        keyboard.append([{"text": "⬆️ Yuqoriga", "callback_data": "tvm:u:0"}])
    keyboard.append([{"text": "🎬 Video Bazaga" if target_root == "base" else "📚 Bazaga",
                      "callback_data": f"tvm:r:{'v' if target_root == 'base' else 'b'}"}])
    keyboard.append([{"text": "❌ Bekor", "callback_data": "tvm:c:0"}])

    set_state(p["id"], {"t": "item_move", "itemId": item_id, "root": target_root, "cur": cur})
    root_label = "📚 Baza" if target_root == "base" else "🎬 Video Baza"
    text = f"📥 Qayerga ko'chiramiz?\n📍 {root_label} / {escape_html(folder_path(p['id'], cur))}"
    if message_id:
        await edit_message_text(chat_id, message_id, text, keyboard)
    else:
        await send_message(chat_id, text, keyboard)


async def show_folder_manager(p, chat_id, root, folder_id, message_id=None):
    code = code_of(root)
    children = child_folders(p["id"], root, folder_id, "id, name, parent_folder_id")
    keyboard = [[{"text": f"📁 {f['name']}", "callback_data": f"sf:{code}:{f['id']}"}] for f in children]
    keyboard.append([{"text": "➕ Papka qo'shish", "callback_data": f"sa:{code}:{folder_id or 'root'}"}])
    if folder_id:
        keyboard.append([{"text": "✏️ Nomini o'zgartirish", "callback_data": f"sr:{folder_id}"},
                         {"text": "📦 Ko'chirish", "callback_data": f"sm:{folder_id}"}])
        keyboard.append([{"text": "🗑 O'chirish", "callback_data": f"sd:{folder_id}"}])
        parent = db.fetchone("SELECT parent_folder_id FROM idea_folders WHERE id = ?", (folder_id,))
        keyboard.append([{"text": "⬅️ Ortga",
                          "callback_data": f"sf:{code}:{(parent or {}).get('parent_folder_id') or 'root'}"}])
    title = "📚 Baza papkalari" if root == "base" else "🎬 Video Baza papkalari"
    text = (f"<b>{title}</b>\n\n📍 {escape_html(folder_path(p['id'], folder_id))}\n\n"
            f"Ichki papkalar: {len(children)}")
    if message_id:
        await edit_message_text(chat_id, message_id, text, keyboard)
    else:
        await send_message(chat_id, text, keyboard)


async def show_move_targets(p, chat_id, folder_id, message_id):
    folder = db.fetchone("SELECT id, name, root_type FROM idea_folders WHERE id = ? AND user_id = ?",
                         (folder_id, p["id"]))
    if not folder:
        return
    all_folders = db.fetchall("SELECT id, name, parent_folder_id FROM idea_folders WHERE user_id = ? "
                              "AND root_type = ? ORDER BY name", (p["id"], folder["root_type"]))
    descendants = {folder_id}
    changed = True
    while changed:
        changed = False
        for f in all_folders:
            if f["id"] not in descendants and f["parent_folder_id"] in descendants:
                descendants.add(f["id"])
                changed = True
    keyboard = [[{"text": "🏠 Ildizga", "callback_data": "smt:root"}]]
    keyboard += [[{"text": f"📁 {f['name']}", "callback_data": f"smt:{f['id']}"}]
                 for f in all_folders if f["id"] not in descendants]
    keyboard.append([{"text": "⬅️ Bekor", "callback_data": f"sf:{code_of(folder['root_type'])}:{folder_id}"}])
    set_state(p["id"], {"t": "folder_move", "folderId": folder_id})
    await edit_message_text(chat_id, message_id,
                            f"📦 <b>{escape_html(folder['name'])}</b> papkasini qayerga ko'chiramiz?", keyboard)


async def show_users(p, chat_id, message_id=None):
    allowed = db.fetchall("SELECT id, telegram_user_id, label FROM idea_allowed_telegram_users ORDER BY created_at")
    profiles = db.fetchall("SELECT telegram_user_id, telegram_username, is_admin FROM idea_profiles "
                           "WHERE telegram_user_id IS NOT NULL")
    lines = "\n".join(
        f"• <code>{u['telegram_user_id']}</code> "
        f"{'@' + escape_html(u['telegram_username']) if u['telegram_username'] else ''} {'👑' if u['is_admin'] else ''}"
        for u in profiles)
    profile_ids = {int(u["telegram_user_id"]) for u in profiles}
    pending = "\n".join(f"• <code>{a['telegram_user_id']}</code> — <i>kutilmoqda</i>"
                        for a in allowed if int(a["telegram_user_id"]) not in profile_ids)
    keyboard = [[{"text": "➕ Foydalanuvchi qo'shish", "callback_data": "u:add:0"}]]
    keyboard += [[{"text": f"🗑 {a['telegram_user_id']}", "callback_data": f"u:del:{a['id']}"}] for a in allowed]
    text = f"<b>👥 Foydalanuvchilar</b>\n\n" + (lines or "<i>yo'q</i>") + ("\n" + pending if pending else "")
    if message_id:
        await edit_message_text(chat_id, message_id, text, keyboard)
    else:
        await send_message(chat_id, text, keyboard)


# ------------------------------------------------------------------ messages

async def handle_stateful_text(p, chat_id, text):
    state = get_state(p["id"])
    if not state:
        return False
    if text.startswith("/") or text == "❌ Bekor":
        set_state(p["id"], None)
        return False

    t = state.get("t")
    if t == "admin_add":
        digits = re.sub(r"\D", "", text)
        set_state(p["id"], None)
        if not digits or not int(digits):
            await send_message(chat_id, "❌ Telegram ID raqam bo'lishi kerak.")
            return True
        tg_id = int(digits)
        if not db.fetchone("SELECT id FROM idea_allowed_telegram_users WHERE telegram_user_id = ?", (tg_id,)):
            _insert("idea_allowed_telegram_users", telegram_user_id=tg_id, added_by=p["id"], created_at=db.now())
        await send_message(chat_id, f"✅ <code>{tg_id}</code> qo'shildi. Endi u /start bossa bot qabul qiladi.")
        return True

    if t == "rem":
        parsed = nlp.parse_reminder_input(text, p["timezone"])
        if not parsed:
            await send_message(chat_id, REMINDER_PROMPT)
            return True
        title = state.get("title") or "Eslatma"
        if state.get("taskId"):
            task = db.fetchone("SELECT title FROM idea_tasks WHERE id = ? AND user_id = ?", (state["taskId"], p["id"]))
            if task:
                title = task["title"]
        rem_id = _insert("idea_reminders", user_id=p["id"], related_type="task" if state.get("taskId") else "note",
                         related_id=state.get("taskId"), title=title, remind_at=parsed["remind_at"],
                         repeat_every_minutes=parsed["repeat_every"], repeat_remaining=parsed["repeat_remaining"],
                         status="pending", **_now_cols())
        set_state(p["id"], None)
        rep = (f"\n🔁 har {round(parsed['repeat_every'] / 60) or 1} soatda, jami {(parsed['repeat_remaining'] or 0) + 1} marta"
               if parsed["repeat_every"] else "")
        await send_message(chat_id, f"⏰ Eslatma qo'yildi: <b>{escape_html(title)}</b>\n"
                                    f"{nlp.local_date_time(parsed['remind_at'], p['timezone'])}{rep}")
        log(p["id"], "reminder_created", "reminder", rem_id, "telegram")
        return True

    if t == "folder_new":
        name = text[:80]
        _insert("idea_folders", user_id=p["id"], root_type=state["root"], parent_folder_id=state.get("parent"),
                name=name, **_now_cols())
        set_state(p["id"], None)
        await flash(chat_id, f"✅ Papka yaratildi: <b>{escape_html(name)}</b>", 2500)
        if state.get("back") == "browse":
            await show_base(p, chat_id, state["root"], state.get("parent"))
        else:
            await show_folder_manager(p, chat_id, state["root"], state.get("parent"))
        return True

    if t == "folder_rename":
        db.execute("UPDATE idea_folders SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                   (text[:80], db.now(), state["folderId"], p["id"]))
        f = db.fetchone("SELECT root_type, parent_folder_id FROM idea_folders WHERE id = ? AND user_id = ?",
                        (state["folderId"], p["id"]))
        set_state(p["id"], None)
        await send_message(chat_id, "✅ Nomi o'zgartirildi.")
        if f:
            await show_folder_manager(p, chat_id, f["root_type"], f["parent_folder_id"])
        return True

    return False


async def handle_message(msg):
    chat_id = msg["chat"]["id"]
    profile = resolve_profile(msg.get("from"), chat_id)
    if not profile:
        await send_message(chat_id, f"Bu shaxsiy bot. Kirish huquqi yo'q.\nSizning Telegram ID: "
                                    f"<code>{(msg.get('from') or {}).get('id')}</code>\n"
                                    f"Admin shu ID ni qo'shsa, bot sizni qabul qiladi.")
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()
    lower = text.lower()

    # Klaviatura tugmalari har doim navigatsiya sifatida ishlaydi (holatdan qat'i nazar)
    if text and is_browse_button(text) and await handle_browse_text(profile, chat_id, text):
        _spawn(delete_message(chat_id, msg["message_id"]))
        return
    if text and await handle_stateful_text(profile, chat_id, text):
        return

    # Asosiy menyu buyruqlari - navigatsiya joyini tozalaymiz
    if lower.startswith("/") or text in MENU_TEXTS:
        set_state(profile["id"], None, None)

    deep_link = re.match(r"^/start\s+v_([0-9a-f-]+)$", lower)
    if deep_link:
        return await show_item_by_link(profile, chat_id, deep_link.group(1))
    if lower.startswith(("/start", "/help", "/yordam")):
        await send_message(chat_id, HELP, None, MAIN_KEYBOARD)
        return
    if lower.startswith("/bugun") or text == "📅 Bugun":
        return await show_today(profile, chat_id)
    if lower.startswith("/ideyalar") or text == "💡 Ideyalar":
        return await show_ideas(profile, chat_id)
    if lower.startswith("/baza") or text == "📚 Baza":
        return await show_base(profile, chat_id, "base")
    if lower.startswith("/video") or text == "🎬 Video Baza":
        return await show_base(profile, chat_id, "video_base")
    if lower.startswith(("/yuklash", "/tarjima")) or text in WEB_UPLOAD_TEXTS:
        return await show_web_upload(profile, chat_id)
    if lower.startswith("/eslatmalar") or text == "⏰ Eslatmalar":
        return await show_reminders(profile, chat_id)
    if lower.startswith("/sozlamalar") or text == "⚙️ Sozlamalar":
        return await show_settings(profile, chat_id)
    if lower.startswith("/qidir"):
        return await search(profile, chat_id, text[6:].strip())
    if lower.startswith(("/foydalanuvchilar", "/odam")):
        if not profile["is_admin"]:
            await send_message(chat_id, "❌ Faqat admin uchun.")
            return
        return await show_users(profile, chat_id)

    # "☁️ Webga yuklash" rejimi: videolar saytning Bulutiga yuklanadi
    state, nav = get_row(profile["id"])
    if state and state.get("t") == "web_upload":
        video = web_upload_video(msg)
        if video:
            _spawn(upload_to_web(chat_id, *video))
        else:
            await send_message(chat_id, "☁️ Hozir «Webga yuklash» rejimidasiz: video yoki zip yuboring yoki "
                                        "menyudan boshqa bo'limni tanlang.")
        return

    media = extract_media(msg)
    url_match = re.search(r"https?://\S+", text)

    # Baza/Video Baza ichida turgan bo'lsak - to'g'ridan-to'g'ri shu papkaga saqlaymiz
    browse_nav = nav if (state is None or state.get("t") == "browse") else None
    if browse_nav:
        item_type = ("link" if url_match else "note") if media["kind"] == "text" else media["kind"]
        title = (text or media["name"] or media["kind"])[:120]
        item_id = _insert("idea_items", user_id=profile["id"], root_type=browse_nav["root"],
                          folder_id=browse_nav["folder"], type=item_type, title=title, content=text or None,
                          url=url_match.group(0) if url_match else None, **_now_cols())
        if media["file_id"]:
            _insert("idea_attachments", user_id=profile["id"], related_type="item", related_id=item_id,
                    file_kind=media["kind"], file_name=media["name"], telegram_file_id=media["file_id"],
                    created_at=db.now())
        _spawn(delete_message(chat_id, msg["message_id"]))
        await flash(chat_id, f"💾 Saqlandi: <b>{escape_html(title)}</b>\n"
                             f"📍 {escape_html(folder_path(profile['id'], browse_nav['folder']))}", 3000)
        return

    # Matnli xabarlar uchun tabiiy til tahlili
    if media["kind"] == "text" and text:
        now = nlp.now_in_tz(profile["timezone"])
        parsed = nlp.parse_message(text, now, nlp.tz_offset_minutes(profile["timezone"]))

        if parsed["kind"] == "reminder" and parsed["remind_at"]:
            rem_id = _insert("idea_reminders", user_id=profile["id"], related_type="note", title=parsed["title"],
                             remind_at=parsed["remind_at"], status="pending", **_now_cols())
            log(profile["id"], "reminder_created", "reminder", rem_id, "telegram")
            _spawn(delete_message(chat_id, msg["message_id"]))
            await flash(chat_id, f"⏰ Eslatma qo'yildi: <b>{escape_html(parsed['title'])}</b>\n"
                                 f"{nlp.local_date_time(parsed['remind_at'], profile['timezone'])}")
            return

        if parsed["kind"] == "task" and parsed["due_date"]:
            task_id = _insert("idea_tasks", user_id=profile["id"], title=parsed["title"],
                              due_date=parsed["due_date"], **_now_cols())
            log(profile["id"], "task_created", "task", task_id, "telegram")
            _spawn(delete_message(chat_id, msg["message_id"]))
            await flash(chat_id, f"✅ Vazifa saqlandi: <b>{escape_html(parsed['title'])}</b>\n"
                                 f"<i>{nlp.fmt_date(parsed['due_date'], nlp.to_iso_date(now))}</i>\n\n/bugun — ro'yxat")
            return

        if parsed["kind"] == "idea":
            idea_id = _insert("idea_ideas", user_id=profile["id"], title=parsed["title"], status="new", **_now_cols())
            log(profile["id"], "idea_created", "idea", idea_id, "telegram")
            _spawn(delete_message(chat_id, msg["message_id"]))
            await flash(chat_id, f"💡 Ideya saqlandi: <b>{escape_html(parsed['title'])}</b>\n\n/ideyalar — ro'yxat")
            return

    # Aks holda - inbox'ga saqlaymiz va toifasini so'raymiz
    inbox_id = _insert("idea_inbox_items", user_id=profile["id"], kind=media["kind"],
                       text=text or media["name"] or None, url=url_match.group(0) if url_match else None,
                       telegram_message_id=msg["message_id"],
                       raw=json.dumps({"file_id": media["file_id"], "file_name": media["name"], "chat_id": chat_id}),
                       status="pending", **_now_cols())
    if media["file_id"]:
        _insert("idea_attachments", user_id=profile["id"], related_type="inbox", related_id=inbox_id,
                file_kind=media["kind"], file_name=media["name"], telegram_file_id=media["file_id"],
                created_at=db.now())
    preview = escape_html(text[:200]) if text else f"<i>{media['kind']}</i>"
    await send_message(chat_id, f"💾 Saqlandi: {preview}\n\nQayerga joylaymiz?", capture_keyboard(inbox_id))


# ------------------------------------------------------------------ callbacks

def move_attachments(user_id, inbox_id, related_type, target_id):
    db.execute("UPDATE idea_attachments SET related_type = ?, related_id = ? WHERE user_id = ? "
               "AND related_type = 'inbox' AND related_id = ?", (related_type, target_id, user_id, inbox_id))


async def _cb_capture(p, cb, chat_id, message_id, action, inbox_id):
    item = db.fetchone("SELECT * FROM idea_inbox_items WHERE id = ? AND user_id = ?", (inbox_id, p["id"]))
    if not item:
        _spawn(answer_callback(cb["id"], "Topilmadi"))
        return
    title = (item["text"] or item["url"] or "Nomsiz")[:120]
    done_text = ""
    if action == "t":
        task_id = _insert("idea_tasks", user_id=p["id"], title=title, description=item["text"],
                          due_date=_today(p), **_now_cols())
        db.execute("UPDATE idea_inbox_items SET status = 'filed', updated_at = ? WHERE id = ?", (db.now(), inbox_id))
        move_attachments(p["id"], inbox_id, "task", task_id)
        log(p["id"], "task_created", "task", task_id, "inbox")
        done_text = f"✅ Vazifa saqlandi: <b>{escape_html(title)}</b>\n/bugun — ro'yxat"
    elif action == "i":
        idea_id = _insert("idea_ideas", user_id=p["id"], title=title, description=item["text"], status="new",
                          **_now_cols())
        db.execute("UPDATE idea_inbox_items SET status = 'filed', updated_at = ? WHERE id = ?", (db.now(), inbox_id))
        move_attachments(p["id"], inbox_id, "idea", idea_id)
        done_text = f"💡 Ideya saqlandi: <b>{escape_html(title)}</b>\n/ideyalar — ro'yxat"
    elif action in ("b", "v"):
        root_type = "base" if action == "b" else "video_base"
        kind = item["kind"]
        item_type = ("link" if item["url"] else "note") if kind == "text" else kind
        new_id = _insert("idea_items", user_id=p["id"], root_type=root_type, type=item_type, title=title,
                         content=item["text"], url=item["url"], **_now_cols())
        db.execute("UPDATE idea_inbox_items SET status = 'filed', updated_at = ? WHERE id = ?", (db.now(), inbox_id))
        move_attachments(p["id"], inbox_id, "item", new_id)
        done_text = f"{'📚 Bazaga' if action == 'b' else '🎬 Video Bazaga'} saqlandi: <b>{escape_html(title)}</b>"
    elif action == "l":
        db.execute("UPDATE idea_inbox_items SET status = 'archived', updated_at = ? WHERE id = ?",
                   (db.now(), inbox_id))
        done_text = f"📥 Keyinroq saqlandi: <b>{escape_html(title)}</b>"

    _spawn(answer_callback(cb["id"], "Saqlandi ✅"))
    # chatni toza qoldiramiz: tanlov kartasi va asl xabar o'chiriladi
    _spawn(delete_message(chat_id, message_id))
    if item["telegram_message_id"]:
        _spawn(delete_message(chat_id, int(item["telegram_message_id"])))
    if done_text:
        await flash(chat_id, done_text)


async def _cb_task(p, cb, chat_id, message_id, action, task_id):
    if action == "d":
        db.execute("UPDATE idea_tasks SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ? "
                   "AND user_id = ?", (db.now(), db.now(), task_id, p["id"]))
        _spawn(delete_message(chat_id, message_id))
        _spawn(answer_callback(cb["id"], "✅ Bajarildi"))
        return
    if action == "x":
        db.execute("DELETE FROM idea_tasks WHERE id = ? AND user_id = ?", (task_id, p["id"]))
        _spawn(delete_message(chat_id, message_id))
        _spawn(answer_callback(cb["id"], "🗑 O'chirildi"))
        return
    if action == "n":
        due = nlp.to_iso_date(nlp.now_in_tz(p["timezone"]) + timedelta(days=1))
        db.execute("UPDATE idea_tasks SET status = 'todo', due_date = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                   (due, db.now(), task_id, p["id"]))
        log(p["id"], "task_deferred", "task", task_id, due)
        await edit_message_text(chat_id, message_id, f"❌ Bajarilmadi — ertaga ({due}) ko'chirildi",
                                task_keyboard(task_id))
    elif action == "r":
        set_state(p["id"], {"t": "rem", "taskId": task_id})
        await send_message(chat_id, REMINDER_PROMPT)
    _spawn(answer_callback(cb["id"], "Tayyor"))


async def _cb_idea(p, cb, chat_id, message_id, action, idea_id):
    if action == "x":
        db.execute("DELETE FROM idea_ideas WHERE id = ? AND user_id = ?", (idea_id, p["id"]))
        _spawn(delete_message(chat_id, message_id))
        _spawn(answer_callback(cb["id"], "🗑 Ideya o'chirildi"))
        return
    if action == "t":
        idea = db.fetchone("SELECT title, description FROM idea_ideas WHERE id = ? AND user_id = ?",
                           (idea_id, p["id"]))
        if idea:
            task_id = _insert("idea_tasks", user_id=p["id"], title=idea["title"], description=idea["description"],
                              due_date=_today(p), idea_id=idea_id, **_now_cols())
            await edit_message_text(chat_id, message_id, f"✅ Vazifaga aylantirildi: <b>{escape_html(idea['title'])}</b>",
                                    task_keyboard(task_id))
    _spawn(answer_callback(cb["id"], "Tayyor"))


async def _cb_item(p, cb, chat_id, message_id, action, item_id):
    if action == "e":
        await edit_reply_markup(chat_id, message_id, item_edit_keyboard(item_id))
    elif action == "b":
        await edit_reply_markup(chat_id, message_id, item_keyboard(item_id))
    elif action == "m":
        it = db.fetchone("SELECT root_type FROM idea_items WHERE id = ? AND user_id = ?", (item_id, p["id"]))
        target_root = "base" if it and it["root_type"] == "base" else "video_base"
        # media xabarni tahrirlab bo'lmaydi - papka tanlash alohida xabar bilan chiqadi
        await edit_reply_markup(chat_id, message_id, item_keyboard(item_id))
        await show_item_move_targets(p, chat_id, item_id, 0, target_root)
    elif action == "x":
        db.execute("DELETE FROM idea_items WHERE id = ? AND user_id = ?", (item_id, p["id"]))
        _spawn(delete_message(chat_id, message_id))
        _spawn(answer_callback(cb["id"], "🗑 O'chirildi"))
        return
    _spawn(answer_callback(cb["id"]))


async def _cb_item_move(p, cb, chat_id, message_id, action, arg):
    state = get_state(p["id"])
    if not state or state.get("t") != "item_move":
        _spawn(answer_callback(cb["id"], "Bekor qilindi"))
        return
    if action == "c":
        set_state(p["id"], None)
        _spawn(delete_message(chat_id, message_id))
        _spawn(answer_callback(cb["id"], "Bekor qilindi"))
        return
    if action == "o" and arg:
        await show_item_move_targets(p, chat_id, state["itemId"], message_id, state["root"], arg)
    elif action == "r":
        await show_item_move_targets(p, chat_id, state["itemId"], message_id,
                                     "base" if arg == "b" else "video_base", None)
    elif action == "u":
        await show_item_move_targets(p, chat_id, state["itemId"], message_id, state["root"],
                                     _folder_parent(p["id"], state.get("cur")))
    elif action == "s":
        db.execute("UPDATE idea_items SET root_type = ?, folder_id = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                   (state["root"], state.get("cur"), db.now(), state["itemId"], p["id"]))
        moved = db.fetchone("SELECT title FROM idea_items WHERE id = ? AND user_id = ?", (state["itemId"], p["id"]))
        set_state(p["id"], None)
        if not moved:
            _spawn(answer_callback(cb["id"], "❌ Ko'chirib bo'lmadi"))
            return
        _spawn(delete_message(chat_id, message_id))
        _spawn(answer_callback(cb["id"], "📥 Ko'chirildi"))
        root_label = "📚 Baza" if state["root"] == "base" else "🎬 Video Baza"
        await flash(chat_id, f"📥 Ko'chirildi: <b>{escape_html(moved['title'])}</b>\n📍 {root_label} / "
                             f"{escape_html(folder_path(p['id'], state.get('cur')))}")
        return
    _spawn(answer_callback(cb["id"]))


async def _cb_reminder(p, cb, chat_id, message_id, action, rem_id, parts):
    if action == "d":
        db.execute("UPDATE idea_reminders SET status = 'done', repeat_remaining = 0, updated_at = ? "
                   "WHERE id = ? AND user_id = ?", (db.now(), rem_id, p["id"]))
        await edit_message_text(chat_id, message_id, "✅ Bajarildi")
    elif action == "x":
        db.execute("DELETE FROM idea_reminders WHERE id = ? AND user_id = ?", (rem_id, p["id"]))
        await edit_message_text(chat_id, message_id, "🗑 Eslatma o'chirildi")
    else:
        try:
            mins = int(parts[3]) if len(parts) > 3 else 10
        except ValueError:
            mins = 10
        rem = db.fetchone("SELECT title FROM idea_reminders WHERE id = ? AND user_id = ?", (rem_id, p["id"]))
        db.execute("UPDATE idea_reminders SET remind_at = ?, status = 'pending', sent_at = NULL, updated_at = ? "
                   "WHERE id = ? AND user_id = ?", (_utc_after(mins), db.now(), rem_id, p["id"]))
        await edit_message_text(chat_id, message_id, f"⏰ {mins} daqiqadan keyin qayta eslataman: "
                                                     f"<b>{escape_html((rem or {}).get('title') or '')}</b>")
    _spawn(answer_callback(cb["id"], "Tayyor"))


async def handle_callback(cb):
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    profile = resolve_profile(cb.get("from"), chat_id)
    if not profile:
        _spawn(answer_callback(cb["id"], "Ruxsat yo'q"))
        return

    parts = str(cb.get("data") or "").split(":")
    ns = parts[0]
    action = parts[1] if len(parts) > 1 else None
    arg = parts[2] if len(parts) > 2 else None

    if ns == "k" and arg:
        return await _cb_capture(profile, cb, chat_id, message_id, action, arg)
    if ns == "t" and arg:
        return await _cb_task(profile, cb, chat_id, message_id, action, arg)
    if ns == "i" and arg:
        return await _cb_idea(profile, cb, chat_id, message_id, action, arg)
    if ns == "tv" and arg:
        return await _cb_item(profile, cb, chat_id, message_id, action, arg)
    if ns == "tg" and action and arg is not None:
        _spawn(answer_callback(cb["id"]))
        return await send_item_group(profile, chat_id, action, arg)
    if ns == "tvm" and action:
        return await _cb_item_move(profile, cb, chat_id, message_id, action, arg)
    if ns == "r" and arg:
        return await _cb_reminder(profile, cb, chat_id, message_id, action, arg, parts)

    if ns == "f":
        await show_base(profile, chat_id, "base" if action == "b" else "video_base", None if arg == "root" else arg)
    elif ns == "sf":
        await show_folder_manager(profile, chat_id, root_of(action), None if arg == "root" else arg, message_id)
    elif ns == "sa":
        set_state(profile["id"], {"t": "folder_new", "root": root_of(action), "parent": None if arg == "root" else arg})
        await send_message(chat_id, "📁 Yangi papka nomini yozing:")
    elif ns == "sr" and action:
        set_state(profile["id"], {"t": "folder_rename", "folderId": action})
        await send_message(chat_id, "✏️ Yangi nomni yozing:")
    elif ns == "sm" and action:
        await show_move_targets(profile, chat_id, action, message_id)
    elif ns == "smt" and action:
        state = get_state(profile["id"])
        if not state or state.get("t") != "folder_move":
            _spawn(answer_callback(cb["id"], "Bekor qilindi"))
            return
        db.execute("UPDATE idea_folders SET parent_folder_id = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                   (None if action == "root" else action, db.now(), state["folderId"], profile["id"]))
        f = db.fetchone("SELECT root_type, parent_folder_id FROM idea_folders WHERE id = ? AND user_id = ?",
                        (state["folderId"], profile["id"]))
        set_state(profile["id"], None)
        if f:
            await show_folder_manager(profile, chat_id, f["root_type"], f["parent_folder_id"], message_id)
        _spawn(answer_callback(cb["id"], "📦 Ko'chirildi"))
        return
    elif ns == "sd" and action:
        f = db.fetchone("SELECT root_type, parent_folder_id FROM idea_folders WHERE id = ? AND user_id = ?",
                        (action, profile["id"]))
        if db.fetchone("SELECT 1 FROM idea_folders WHERE parent_folder_id = ? LIMIT 1", (action,)):
            _spawn(answer_callback(cb["id"], "Avval ichki papkalarni o'chiring"))
            return
        db.execute("UPDATE idea_items SET folder_id = NULL WHERE folder_id = ? AND user_id = ?", (action, profile["id"]))
        db.execute("DELETE FROM idea_folders WHERE id = ? AND user_id = ?", (action, profile["id"]))
        if f:
            await show_folder_manager(profile, chat_id, f["root_type"], f["parent_folder_id"], message_id)
        _spawn(answer_callback(cb["id"], "🗑 O'chirildi"))
        return
    elif ns == "u":
        if not profile["is_admin"]:
            _spawn(answer_callback(cb["id"], "Faqat admin uchun"))
            return
        if action == "add":
            set_state(profile["id"], {"t": "admin_add"})
            await send_message(chat_id, "👤 Yangi foydalanuvchining Telegram ID sini yozing "
                                        "(masalan <code>123456789</code>):")
        elif action == "del" and arg:
            db.execute("DELETE FROM idea_allowed_telegram_users WHERE id = ?", (arg,))
            await show_users(profile, chat_id, message_id)
        else:
            await show_users(profile, chat_id, message_id)
    _spawn(answer_callback(cb["id"]))


# ------------------------------------------------------------------ scheduled

async def run_due_reminders():
    now = db.now()
    due = db.fetchall("SELECT id, user_id, title, repeat_every_minutes, repeat_remaining FROM idea_reminders "
                      "WHERE status = 'pending' AND remind_at <= ? LIMIT 50", (now,))
    for r in due:
        chats = chats_for_owner(r["user_id"])
        if not chats:
            continue
        for chat in chats:
            await send_message(chat, f"⏰ <b>{escape_html(r['title'])}</b>", [
                [{"text": "✅ Bajarildi", "callback_data": f"r:d:{r['id']}"},
                 {"text": "⏰ 10 daq", "callback_data": f"r:s:{r['id']}:10"}],
                [{"text": "⏰ 1 soat", "callback_data": f"r:s:{r['id']}:60"},
                 {"text": "⏰ Ertaga", "callback_data": f"r:s:{r['id']}:1440"}],
            ])
        if r["repeat_every_minutes"] and (r["repeat_remaining"] or 0) > 0:
            db.execute("UPDATE idea_reminders SET remind_at = ?, repeat_remaining = ?, status = 'pending', "
                       "sent_at = ?, updated_at = ? WHERE id = ?",
                       (_utc_after(r["repeat_every_minutes"]), (r["repeat_remaining"] or 1) - 1, now, now, r["id"]))
        else:
            db.execute("UPDATE idea_reminders SET status = 'sent', sent_at = ?, updated_at = ? WHERE id = ?",
                       (now, now, r["id"]))
    return len(due)


async def run_daily_reviews():
    sent = 0
    for p in db.fetchall("SELECT * FROM idea_profiles WHERE daily_review_enabled = 1 "
                         "AND telegram_chat_id IS NOT NULL"):
        local = nlp.now_in_tz(p["timezone"])
        today = nlp.to_iso_date(local)
        if p["last_daily_review_on"] == today:
            continue
        hh, mm = (str(p["daily_review_time"] or "19:00").split(":") + ["0"])[:2]
        if local.hour * 60 + local.minute < int(hh) * 60 + int(mm):
            continue

        local_midnight = datetime.combine(local.date(), datetime.min.time())
        since = nlp.utc_str(nlp.local_to_utc(local_midnight, nlp.tz_offset_minutes(p["timezone"])))
        done = db.fetchall("SELECT title FROM idea_tasks WHERE user_id = ? AND status = 'done' AND completed_at >= ?",
                           (p["id"], since))
        open_tasks = db.fetchall("SELECT id, title, due_date FROM idea_tasks WHERE user_id = ? AND status != 'done' "
                                 "AND due_date <= ?", (p["id"], today))
        tomorrow = nlp.to_iso_date(local + timedelta(days=1))
        done_lines = "\n".join(f"✅ {escape_html(t['title'])}" for t in done) or "<i>yo'q</i>"
        open_lines = "\n".join(f"• {escape_html(t['title'])}" for t in open_tasks) or "<i>yo'q</i>"
        chats = chats_for_owner(p["id"])
        for chat in chats:
            await send_message(chat, f"<b>🌙 Kunlik xulosa — {today}</b>\n\n<b>Bajarildi</b>\n{done_lines}\n\n"
                                     f"<b>Bajarilmadi (ertaga {tomorrow} ga ko'chirildi)</b>\n{open_lines}")
        for t in open_tasks:
            db.execute("UPDATE idea_tasks SET due_date = ?, updated_at = ? WHERE id = ?", (tomorrow, db.now(), t["id"]))
            for chat in chats:
                await send_message(chat, f"• <b>{escape_html(t['title'])}</b>", task_keyboard(t["id"]))
        db.execute("UPDATE idea_profiles SET last_daily_review_on = ? WHERE id = ?", (today, p["id"]))
        sent += 1
    return sent


# ------------------------------------------------------------------ runtime

async def _handle_update(update):
    if update.get("callback_query"):
        await handle_callback(update["callback_query"])
    elif update.get("message") or update.get("edited_message"):
        await handle_message(update.get("message") or update.get("edited_message"))


async def _poll():
    try:
        me = await tg("getMe")
        print(f"[ideaflow_bot] Ulandi: @{me.get('username')} (id={me.get('id')})", flush=True)
        db.set_setting(BOT_USERNAME_KEY, me.get("username") or "")
        # Lovable webhook'ini o'chiramiz - aks holda getUpdates ishlamaydi.
        await tg("deleteWebhook")
        await tg("setMyCommands", {"commands": [
            {"command": "bugun", "description": "Bugungi vazifalar"},
            {"command": "ideyalar", "description": "Ideyalar"},
            {"command": "baza", "description": "Bilim bazasi"},
            {"command": "video", "description": "Video baza"},
            {"command": "yuklash", "description": "Saytga video yuklash"},
            {"command": "eslatmalar", "description": "Eslatmalar"},
            {"command": "qidir", "description": "Qidiruv"},
            {"command": "sozlamalar", "description": "Sozlamalar va papkalar"},
            {"command": "yordam", "description": "Yordam"},
        ]})
    except Exception as e:
        print(f"[ideaflow_bot] XATO: botga ulanib bo'lmadi (IDEA_BOT_TOKEN tekshiring): {e}", flush=True)

    offset = int(db.get_setting(LAST_UPDATE_ID_KEY, "0") or "0")
    async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 15) as client:
        while True:
            try:
                resp = await client.post(f"{IDEA_BOT_API_URL.rstrip('/')}/bot{IDEA_BOT_TOKEN}/getUpdates", json={
                    "offset": offset, "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message", "edited_message", "callback_query"],
                })
                data = resp.json()
                if not data.get("ok"):
                    print(f"[ideaflow_bot] getUpdates xatosi: {resp.text[:300]}", flush=True)
                    await asyncio.sleep(10)
                    continue
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    db.set_setting(LAST_UPDATE_ID_KEY, str(offset))
                    try:
                        await _handle_update(update)
                    except Exception:
                        print("[ideaflow_bot] XATO xabarni qayta ishlashda:", flush=True)
                        traceback.print_exc()
            except Exception:
                print("[ideaflow_bot] XATO polling tsiklida:", flush=True)
                traceback.print_exc()
                await asyncio.sleep(5)


async def _scheduler():
    """Eslatmalar va kunlik hisobot - har daqiqada (Lovable'dagi pg_cron o'rniga)."""
    while True:
        try:
            await run_due_reminders()
            await run_daily_reviews()
        except Exception:
            print("[ideaflow_bot] XATO eslatma/hisobot tsiklida:", flush=True)
            traceback.print_exc()
        await asyncio.sleep(60)


def start():
    if not IDEA_BOT_TOKEN:
        print("[ideaflow_bot] IDEA_BOT_TOKEN sozlanmagan - Idea Flow boti o'chirilgan.", flush=True)
        return
    _spawn(_poll())
    _spawn(_scheduler())
