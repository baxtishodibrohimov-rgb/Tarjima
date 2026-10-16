"""Kutubxona: papka ichidagi videolar tartibi."""
import pytest
from fastapi.testclient import TestClient

import app as app_module
import database as db


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


def test_reorder_inside_folder_and_move_resets(client):
    folder = client.post("/api/folders", data={"name": "Tartib"}).json()["id"]
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    ids = []
    for i in range(3):
        vid = db.new_id()
        db.execute("INSERT INTO videos (id, original_name, status, owner_id, folder_id, created_at, updated_at) "
                   "VALUES (?, ?, 'uploaded', ?, ?, ?, ?)",
                   (vid, f"dars{i}.mp4", owner, folder, f"2026-01-0{i + 1}T00:00:00", db.now()))
        ids.append(vid)

    def order():
        return [v["id"] for v in client.get("/api/videos").json() if v["folder_id"] == folder]

    assert order() == ids[::-1]  # standart: yangisi birinchi
    wanted = [ids[1], ids[0], ids[2]]
    assert client.post("/api/videos/reorder", json={"folder_id": folder, "ids": wanted}).status_code == 200
    assert order() == wanted
    # boshqa papkadagi video bilan aralashtirib bo'lmaydi
    assert client.post("/api/videos/reorder", json={"folder_id": None, "ids": wanted}).status_code == 400
    # yangi ko'chirilgan video papkada eng yuqorida
    new = db.new_id()
    db.execute("INSERT INTO videos (id, original_name, status, owner_id, created_at, updated_at) "
               "VALUES (?, 'yangi.mp4', 'uploaded', ?, ?, ?)", (new, owner, db.now(), db.now()))
    assert client.post(f"/api/videos/{new}/folder", data={"folder_id": folder}).status_code == 200
    assert order() == [new] + wanted


def test_default_order_follows_file_date_and_reset(client):
    folder = client.post("/api/folders", data={"name": "Sana"}).json()["id"]
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    # yuklangan vaqti boshqacha, lekin kompyuterdagi fayl sanasi bo'yicha tartib kerak
    rows = [("a.mp4", "2026-10-16T10:00:00", "2026-10-07T09:23:00"),
            ("b.mp4", "2026-10-16T10:01:00", "2026-10-13T15:27:00"),
            ("c.mp4", "2026-10-16T10:02:00", "2026-10-10T14:17:00")]
    ids = {}
    for name, created, mtime in rows:
        vid = db.new_id()
        ids[name] = vid
        db.execute("INSERT INTO videos (id, original_name, status, owner_id, folder_id, source_mtime, created_at, "
                   "updated_at) VALUES (?, ?, 'uploaded', ?, ?, ?, ?, ?)", (vid, name, owner, folder, mtime, created,
                                                                          created))

    def order():
        return [v["original_name"] for v in client.get("/api/videos").json() if v["folder_id"] == folder]

    assert order() == ["b.mp4", "c.mp4", "a.mp4"]
    client.post("/api/videos/reorder", json={"folder_id": folder, "ids": [ids["a.mp4"], ids["b.mp4"], ids["c.mp4"]]})
    assert order() == ["a.mp4", "b.mp4", "c.mp4"]
    assert client.post("/api/videos/reorder-by-date", json={"folder_id": folder}).status_code == 200
    assert order() == ["b.mp4", "c.mp4", "a.mp4"]


def test_upload_keeps_last_modified(client):
    import time as _t
    ms = int(_t.mktime((2026, 10, 13, 15, 27, 0, 0, 0, 0)) * 1000)
    r = client.post("/api/videos/upload/init", data={"original_name": "mtime_test.mp4", "total_size": "10",
                                                     "kind": "pipeline", "last_modified": str(ms)})
    up = db.fetchone("SELECT source_mtime FROM uploads WHERE id = ?", (r.json()["upload_id"],))
    assert up["source_mtime"] == _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(ms / 1000))
