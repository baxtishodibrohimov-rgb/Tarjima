import io
import struct
import time
import zipfile
import zlib

import pytest
from fastapi.testclient import TestClient

import app as app_module
import bot_section
import cloud_zip
import database as db


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


def upload(client, name, data, kind):
    init = client.post("/api/videos/upload/init", data={"original_name": name, "total_size": len(data),
                                                        "kind": "cloud", "file_kind": kind}).json()
    client.post(f"/api/videos/upload/{init['upload_id']}/chunk", data={"offset": 0}, files={"chunk": ("c", data)})
    return client.post(f"/api/videos/upload/{init['upload_id']}/complete").json()["cloud_file_id"]


class FakeTelegram:
    sent = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, data=None, files=None):
        field = next(iter(files))
        FakeTelegram.sent.append((data, files[field][0], url.rsplit("/", 1)[-1]))
        file_id = f"F{len(FakeTelegram.sent)}"

        class R:
            text = "ok"

            def json(self):
                return {"ok": True, "result": {field: {"file_id": file_id}}}
        return R()


def test_cloud_video_goes_to_bot_and_leaves_bulut(client, monkeypatch):
    monkeypatch.setattr(bot_section, "IDEA_BOT_TOKEN", "T")
    monkeypatch.setattr(bot_section.httpx, "AsyncClient", FakeTelegram)
    if not db.fetchone("SELECT 1 FROM idea_profiles WHERE telegram_user_id = 777"):
        db.execute("INSERT INTO idea_profiles (id, telegram_user_id, telegram_chat_id, is_admin, created_at) "
                   "VALUES ('owner-777', 777, 777, 1, ?)", (db.now(),))
    folder = client.post("/api/bot/folders", data={"name": "Ortodontiya"}).json()["id"]
    cid = upload(client, "Marco Rosa.mp4", b"\0" * 5000, "video")

    r = client.post("/api/bot/upload-cloud", data={"cloud_file_id": cid, "folder_id": folder,
                                                   "remove_from_library": "true"})
    assert r.status_code == 200
    for _ in range(100):
        if not db.fetchone("SELECT 1 FROM cloud_files WHERE id = ?", (cid,)):
            break
        time.sleep(0.05)
    assert not db.fetchone("SELECT 1 FROM cloud_files WHERE id = ?", (cid,))  # Bulutdan o'chdi
    item = next(i for i in client.get("/api/bot/tree").json()["items"] if i["title"] == "Marco Rosa")
    assert item["folder_id"] == folder and len(item["parts"]) == 1
    assert "Ortodontiya" in FakeTelegram.sent[-1][0]["caption"]


def test_zip_unicode_path_field_and_rename(client):
    real = "Marco Rosa - Лечение брекетами.mp4"
    shown = real.encode("ascii", "replace").decode()
    info = zipfile.ZipInfo(shown)
    info.extra = (struct.pack("<HHB", 0x7075, 5 + len(real.encode()), 1)
                  + struct.pack("<I", zlib.crc32(shown.encode("cp437"))) + real.encode())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, b"video")
    zid = upload(client, "kurs.zip", buf.getvalue(), "zip")
    entries = client.get(f"/api/cloud-files/{zid}/zip").json()["entries"]
    assert [e["name"] for e in entries] == [real]

    r = client.post(f"/api/cloud-files/{zid}/rename", data={"name": "Ortodontiya kursi"})
    assert r.json()["original_name"] == "Ortodontiya kursi.zip"
    assert client.post(f"/api/cloud-files/{zid}/rename", data={"name": "  "}).status_code == 400


def test_zip_names_from_local_header_when_central_is_question_marks(client):
    """Serverdagi haqiqiy zip kabi: markaziy ro'yxatda "???", lokal sarlavhada
    cp866 nom, 0x7075 maydonida esa UTF-8 bo'lmagan baytlar."""
    names = ["Макаревич Иван/Десятая лекция.mp4", "Макаревич Иван/Конспект.pdf"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            raw = n.encode("cp866")
            info = zipfile.ZipInfo("?" * len(raw))
            bad = struct.pack("<B", 1) + struct.pack("<I", 0) + raw  # UTF-8 emas
            info.extra = struct.pack("<HH", 0x7075, len(bad)) + bad
            zf.writestr(info, b"data")
    data = bytearray(buf.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        for info, n in zip(zf.infolist(), names):
            raw = n.encode("cp866")
            start = info.header_offset + 30
            data[start:start + len(raw)] = raw  # lokal sarlavhaga to'g'ri nom
    zid = upload(client, "Макаревич.zip", bytes(data), "zip")
    entries = client.get(f"/api/cloud-files/{zid}/zip").json()["entries"]
    assert [e["path"] for e in entries] == names
    r = client.get(f"/api/cloud-files/{zid}/zip/download", params={"index": entries[1]["index"]})
    assert r.content == b"data"
    client.post(f"/api/cloud-files/{zid}/zip/extract", json={"mode": "videos"})
    for _ in range(100):
        if db.fetchone("SELECT 1 FROM cloud_files WHERE original_name = ?", ("Макаревич Иван - Десятая лекция.mp4",)):
            break
        time.sleep(0.05)
    assert db.fetchone("SELECT 1 FROM cloud_files WHERE original_name = ?", ("Макаревич Иван - Десятая лекция.mp4",))


def test_whole_project_goes_to_bot_as_one_item(client, monkeypatch):
    import ideaflow_bot
    from storage import RESULTS_DIR, VIDEOS_DIR
    monkeypatch.setattr(bot_section, "IDEA_BOT_TOKEN", "T")
    monkeypatch.setattr(bot_section.httpx, "AsyncClient", FakeTelegram)
    if not db.fetchone("SELECT 1 FROM idea_profiles WHERE telegram_user_id = 777"):
        db.execute("INSERT INTO idea_profiles (id, telegram_user_id, telegram_chat_id, is_admin, created_at) "
                   "VALUES ('owner-777', 777, 777, 1, ?)", (db.now(),))
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    vid = db.new_id()
    (VIDEOS_DIR / vid).mkdir(parents=True)
    (RESULTS_DIR / vid).mkdir(parents=True)
    orig, final, mp3 = VIDEOS_DIR / vid / "Dars.mp4", RESULTS_DIR / vid / "final.mp4", RESULTS_DIR / vid / "a.mp3"
    for p, data in ((orig, b"o" * 100), (final, b"f" * 100), (mp3, b"ID3")):
        p.write_bytes(data)
    srt = RESULTS_DIR / vid / "Dars.uz.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nSalom\n")
    db.execute("INSERT INTO results (id, video_id, kind, filename, path) VALUES (?, ?, 'srt_uz', ?, ?)",
               (db.new_id(), vid, srt.name, str(srt)))
    db.execute("INSERT INTO videos (id, owner_id, original_name, path, status, final_video_path, audio_path, "
               "created_at, updated_at) VALUES (?, ?, 'Dars.mp4', ?, 'completed', ?, ?, ?, ?)",
               (vid, owner, str(orig), str(final), str(mp3), db.now(), db.now()))
    FakeTelegram.sent.clear()
    r = client.post("/api/bot/upload", data={"video_id": vid, "variant": "project", "remove_from_library": "false"})
    assert r.json()["files"] == 4
    for _ in range(100):
        if db.fetchone("SELECT bot_upload_status s FROM videos WHERE id = ?", (vid,))["s"] != "uploading":
            break
        time.sleep(0.05)
    assert [m for _, _, m in FakeTelegram.sent] == ["sendVideo", "sendVideo", "sendDocument", "sendAudio"]
    assert FakeTelegram.sent[1][1] == "Dars — O'zbekcha video.mp4"
    item_id = db.fetchone("SELECT bot_item_id FROM videos WHERE id = ?", (vid,))["bot_item_id"]
    groups = ideaflow_bot.item_groups(item_id)
    assert [g[1] for g in groups] == ["🎬 Asl video", "🇺🇿 O'zbekcha video", "📄 O'zbekcha tarjima (SRT)",
                                      "🎵 O'zbekcha dublyaj audio"]
    buttons = [b["text"] for row in ideaflow_bot.item_keyboard(item_id) for b in row]
    assert buttons[:3] == ["🇺🇿 O'zbekcha video", "📄 O'zbekcha tarjima (SRT)", "🎵 O'zbekcha dublyaj audio"]
