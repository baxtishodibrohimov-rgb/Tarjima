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
