import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database as db
from storage import CHUNKS_DIR, RESULTS_DIR, SPLIT_DIR, TTS_DIR, VIDEOS_DIR


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


def make_project():
    """Barcha turdagi hosilalari bor loyiha: asosiy TTS, ikkinchi provayder treki,
    avvalgi (almashtirilgan) TTS ishi va disk papkalari."""
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    vid, main_job, track_job, old_job = (db.new_id() for _ in range(4))
    db.execute("INSERT INTO videos (id, original_name, status, owner_id, tts_job_id, created_at, updated_at) "
               "VALUES (?, 'dars.mp4', 'completed', ?, ?, ?, ?)", (vid, owner, main_job, db.now(), db.now()))
    for job in (main_job, track_job, old_job):
        db.execute("INSERT INTO tts_jobs (id, title, video_id, created_at) VALUES (?, 't', ?, ?)", (job, vid, db.now()))
        db.execute("INSERT INTO tts_segments (id, job_id) VALUES (?, ?)", (db.new_id(), job))
        (TTS_DIR / job).mkdir(parents=True)
        (TTS_DIR / job / "audio.mp3").write_bytes(b"x" * 100)
    db.execute("INSERT INTO tts_jobs (id, title, video_id, created_at) VALUES (?, 'old', ?, ?)",
               (db.new_id(), None, db.now()))  # videosiz TTS ishi - tegilmasligi kerak
    db.execute("INSERT INTO audio_tracks (id, video_id, provider, tts_job_id) VALUES (?, ?, 'openai', ?)",
               (db.new_id(), vid, track_job))
    db.execute("INSERT INTO results (id, video_id) VALUES (?, ?)", (db.new_id(), vid))
    for base in (VIDEOS_DIR, CHUNKS_DIR, RESULTS_DIR, SPLIT_DIR):
        (base / vid).mkdir(parents=True)
        (base / vid / "f.bin").write_bytes(b"x")
    return vid, (main_job, track_job, old_job)


def test_delete_removes_every_derived_file(client):
    vid, jobs = make_project()
    assert client.delete(f"/api/videos/{vid}").status_code == 200
    for base in (VIDEOS_DIR, CHUNKS_DIR, RESULTS_DIR, SPLIT_DIR):
        assert not (base / vid).exists()
    for job in jobs:
        assert not (TTS_DIR / job).exists()
        assert not db.fetchone("SELECT 1 FROM tts_jobs WHERE id = ?", (job,))
        assert not db.fetchone("SELECT 1 FROM tts_segments WHERE job_id = ?", (job,))
    for table in ("audio_tracks", "results", "videos"):
        col = "id" if table == "videos" else "video_id"
        assert not db.fetchone(f"SELECT 1 FROM {table} WHERE {col} = ?", (vid,))
    assert db.fetchone("SELECT 1 FROM tts_jobs WHERE video_id IS NULL")


def test_costs_survive_delete_for_reports(client):
    vid, _ = make_project()
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    before = client.get("/api/costs").json()["all_time"]["usd"]
    db.execute("INSERT INTO costs (id, video_id, kind, amount_usd, created_at, owner_id) "
               "VALUES (?, ?, 'translation', 1.25, ?, ?)", (db.new_id(), vid, db.now(), owner))
    client.delete(f"/api/videos/{vid}")
    costs = client.get("/api/costs").json()
    assert costs["all_time"]["usd"] == pytest.approx(before + 1.25)
    row = next(r for r in costs["per_video"] if r["id"] == vid)
    assert row["deleted"] and row["original_name"] == "dars.mp4 (o'chirilgan)" and row["translation"] == 1.25


def test_cleanup_script_finds_leftovers_of_old_deletes(client, capsys):
    vid, jobs = make_project()
    db.execute("DELETE FROM videos WHERE id = ?", (vid,))  # eski versiyadagi to'liq bo'lmagan o'chirish
    spec = importlib.util.spec_from_file_location(
        "cleanup_orphans", Path(__file__).resolve().parent.parent / "scripts" / "cleanup_orphans.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    script.main(apply=False)
    out = capsys.readouterr().out
    assert str(TTS_DIR / jobs[1]) in out and "Hech narsa o'chirilmadi" in out
    assert (TTS_DIR / jobs[1]).exists()

    script.main(apply=True)
    assert not (TTS_DIR / jobs[1]).exists() and not (RESULTS_DIR / vid).exists()
    assert not db.fetchone("SELECT 1 FROM audio_tracks WHERE video_id = ?", (vid,))
    assert db.fetchone("SELECT 1 FROM tts_jobs WHERE video_id IS NULL")
