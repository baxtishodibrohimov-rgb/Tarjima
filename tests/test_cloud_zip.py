import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import cloud_zip
import database as db
from storage import CLOUD_DIR

pytestmark = pytest.mark.skipif(not shutil.which("zip"), reason="zip CLI kerak")


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


@pytest.fixture(scope="module")
def sample_zip(tmp_path_factory):
    """Windows'dagidek: kirillcha nom cp866'da (UTF-8 belgisiz), ichma-ich papka,
    __MACOSX axlati, parolli fayl va "../" yo'lli zararli fayl."""
    root = tmp_path_factory.mktemp("zipsrc")
    src = root / "Kurs"
    (src / "1-modul").mkdir(parents=True)
    (src / "__MACOSX").mkdir()
    (src / "1-modul" / "intro.mp4").write_bytes(b"\x00" * 4096)
    (src / "__MACOSX" / "._intro.mp4").write_bytes(b"junk")
    cp866_name = os.fsencode(src / "1-modul") + b"/" + "Дарс 2.mp4".encode("cp866")
    with open(cp866_name, "wb") as f:
        f.write(b"\x01" * 2048)
    out = root / "kurs.zip"
    # Avval Python (UTF-8 nomlar), keyin zip CLI qo'shadi - CLI eski yozuvlarni
    # o'zgartirmaydi (Python "a" rejimi esa cp866 nomni UTF-8 qilib qayta yozardi).
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("Kurs/Урок.srt", "1\n00:00:01,000 --> 00:00:02,000\nSalom\n")
        zf.writestr("../../evil.txt", "hack")
    subprocess.run(["zip", "-qr", str(out), "Kurs"], cwd=root, check=True)
    (root / "secret.txt").write_text("maxfiy")
    subprocess.run(["zip", "-q", "-P", "parol", str(out), "secret.txt"], cwd=root, check=True)
    return out


def upload_zip(client, path: Path) -> str:
    data = path.read_bytes()
    init = client.post("/api/videos/upload/init", data={"original_name": path.name, "total_size": len(data),
                                                        "kind": "cloud", "file_kind": "zip"}).json()
    client.post(f"/api/videos/upload/{init['upload_id']}/chunk", data={"offset": 0}, files={"chunk": ("c", data)})
    return client.post(f"/api/videos/upload/{init['upload_id']}/complete").json()["cloud_file_id"]


def wait_extract(client, cloud_id, secs=20):
    for _ in range(secs * 10):
        row = db.fetchone("SELECT extract_status FROM cloud_files WHERE id = ?", (cloud_id,))
        if not row or row["extract_status"] != "extracting":
            return row
        time.sleep(0.1)
    raise AssertionError("chiqarish tugamadi")


def test_zip_browse_extract_download(client, sample_zip, monkeypatch):
    zid = upload_zip(client, sample_zip)
    card = next(f for f in client.get("/api/cloud-files").json() if f["id"] == zid)
    assert card["kind"] == "zip" and card["zip_entry_count"] == 5

    entries = {e["path"]: e for e in client.get(f"/api/cloud-files/{zid}/zip").json()["entries"]}
    assert set(entries) == {"Kurs/1-modul/intro.mp4", "Kurs/1-modul/Дарс 2.mp4", "secret.txt", "Kurs/Урок.srt",
                            "../../evil.txt"}
    assert entries["secret.txt"]["encrypted"] and entries["Kurs/1-modul/Дарс 2.mp4"]["kind"] == "video"

    r = client.get(f"/api/cloud-files/{zid}/zip/download", params={"index": entries["Kurs/Урок.srt"]["index"]})
    assert r.status_code == 200 and b"Salom" in r.content and "filename*=UTF-8''" in r.headers["content-disposition"]

    r = client.post(f"/api/cloud-files/{zid}/zip/extract", json={"indices": [entries["secret.txt"]["index"]]})
    assert r.status_code == 400 and "Parol" in r.json()["detail"]

    monkeypatch.setattr(cloud_zip, "has_space_for", lambda n: False)
    assert client.post(f"/api/cloud-files/{zid}/zip/extract", json={"mode": "all"}).status_code == 400
    monkeypatch.undo()

    assert client.post(f"/api/cloud-files/{zid}/zip/extract", json={"mode": "videos"}).json()["count"] == 2
    assert wait_extract(client, zid)["extract_status"] == "done"
    videos = {f["original_name"]: f for f in client.get("/api/cloud-files?kind=video").json()}
    assert "1-modul - Дарс 2.mp4" in videos and "1-modul - intro.mp4" in videos
    assert videos["1-modul - Дарс 2.mp4"]["file_size"] == 2048

    # "../../evil.txt" Bulut papkasi ichida, oddiy nom bilan qoladi
    idx = [entries["../../evil.txt"]["index"], entries["Kurs/Урок.srt"]["index"]]
    client.post(f"/api/cloud-files/{zid}/zip/extract", json={"indices": idx, "delete_zip": True})
    assert wait_extract(client, zid) is None  # zip o'chirildi
    files = {f["original_name"]: f for f in client.get("/api/cloud-files?kind=file").json()}
    evil = db.fetchone("SELECT path FROM cloud_files WHERE id = ?", (files["evil.txt"]["id"],))["path"]
    assert Path(evil).resolve().is_relative_to(CLOUD_DIR.resolve()) and "Kurs - Урок.srt" in files
    assert client.get(f"/api/cloud-files/{files['Kurs - Урок.srt']['id']}/download").text.startswith("1\n")


def test_broken_zip_is_reported(client, tmp_path):
    bad = tmp_path / "buzuq.zip"
    bad.write_bytes(b"bu zip emas")
    zid = upload_zip(client, bad)
    card = next(f for f in client.get("/api/cloud-files").json() if f["id"] == zid)
    assert card["extract_status"] == "error" and "buzilgan" in card["extract_error"]
