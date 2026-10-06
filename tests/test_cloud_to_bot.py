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
        FakeTelegram.sent.append((data, files["video"][0]))

        class R:
            text = "ok"

            def json(self):
                return {"ok": True, "result": {"video": {"file_id": f"F{len(FakeTelegram.sent)}"}}}
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
