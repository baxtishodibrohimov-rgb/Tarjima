import os
import random

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database as db

CHUNK = 64 * 1024


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


def init(client, name, data):
    return client.post("/api/videos/upload/init", data={"original_name": name, "total_size": len(data),
                                                        "kind": "cloud", "file_kind": "file"}).json()


def send(client, upload_id, data, offset):
    return client.post(f"/api/videos/upload/{upload_id}/chunk", data={"offset": offset},
                       files={"chunk": ("c", data[offset:offset + CHUNK])})


def cloud_bytes(cloud_id):
    return open(db.fetchone("SELECT path FROM cloud_files WHERE id = ?", (cloud_id,))["path"], "rb").read()


def test_chunks_in_any_order_and_resume(client):
    data = os.urandom(CHUNK * 7 + 1234)
    offsets = list(range(0, len(data), CHUNK))
    first = init(client, "tartibsiz.bin", data)
    random.Random(1).shuffle(offsets)
    for o in offsets[:5]:
        assert send(client, first["upload_id"], data, o).status_code == 200
    # Tugamagan fayl qabul qilinmaydi
    assert client.post(f"/api/videos/upload/{first['upload_id']}/complete").status_code == 400

    # Brauzer yopilib, fayl qayta tanlandi: server qaysi bo'laklar borligini aytadi
    again = init(client, "tartibsiz.bin", data)
    assert again["resumed"] and again["upload_id"] == first["upload_id"]
    have = {start for start, _ in again["received_ranges"]}
    assert have == set(offsets[:5])
    for o in offsets[5:]:
        send(client, first["upload_id"], data, o)
    send(client, first["upload_id"], data, offsets[0])  # takroriy (javobi yo'qolgan) bo'lak zarar qilmaydi
    done = client.post(f"/api/videos/upload/{first['upload_id']}/complete").json()
    assert cloud_bytes(done["cloud_file_id"]) == data
    assert not db.fetchone("SELECT 1 FROM upload_chunks WHERE upload_id = ?", (first["upload_id"],))


def test_old_sequential_upload_resumes(client):
    """Yangilanishdan oldin boshlangan (ketma-ket) yuklash ham davom etadi."""
    data = os.urandom(CHUNK * 3)
    up = init(client, "eski.bin", data)
    with open(db.fetchone("SELECT tmp_path FROM uploads WHERE id = ?", (up["upload_id"],))["tmp_path"], "wb") as f:
        f.write(data[:CHUNK * 2])
    db.execute("UPDATE uploads SET received_size = ? WHERE id = ?", (CHUNK * 2, up["upload_id"]))
    again = init(client, "eski.bin", data)
    assert again["received_ranges"] == [[0, CHUNK * 2]]
    send(client, up["upload_id"], data, CHUNK * 2)
    done = client.post(f"/api/videos/upload/{up['upload_id']}/complete").json()
    assert cloud_bytes(done["cloud_file_id"]) == data


def test_chunk_outside_file_is_rejected(client):
    data = os.urandom(CHUNK)
    up = init(client, "chegara.bin", data)
    assert send(client, up["upload_id"], data, len(data)).status_code == 400
    big = client.post(f"/api/videos/upload/{up['upload_id']}/chunk", data={"offset": CHUNK // 2},
                      files={"chunk": ("c", os.urandom(CHUNK))})
    assert big.status_code == 400
