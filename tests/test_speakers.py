"""Spikerlar: OpenAI diarize, [spk:N], har spikerga ovoz (2.8, 2.9)."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database as db
import stt_diarize
import translation
import tts
import worker


def test_assign_speakers_by_overlap():
    words = [{"w": "a", "s": 0.0, "e": 0.5}, {"w": "b", "s": 0.9, "e": 1.4}, {"w": "c", "s": 5.0, "e": 5.2}]
    segs = [{"start": 0.0, "end": 1.0, "speaker": 1}, {"start": 1.0, "end": 3.0, "speaker": 2}]
    assert [w["spk"] for w in stt_diarize.assign_speakers(words, segs)] == [1, 2, 2]


def test_diarize_chunks_matches_speakers_across_chunks(monkeypatch, tmp_path):
    calls = []

    async def fake_diarize(client, path, api_key, language="", known=None):
        calls.append(known)
        if not known:
            return [{"start": 0, "end": 4, "speaker": "A"}, {"start": 4, "end": 9, "speaker": "B"}]
        return [{"start": 0, "end": 3, "speaker": "spk2"}, {"start": 3, "end": 6, "speaker": "spk1"},
                {"start": 6, "end": 8, "speaker": "C"}]

    monkeypatch.setattr(stt_diarize, "diarize_file", fake_diarize)
    monkeypatch.setattr(stt_diarize, "speaker_reference", lambda *a: "data:audio/mpeg;base64,AA==")
    monkeypatch.setattr(stt_diarize.transcription, "get_duration_seconds", lambda p: 300.0)
    segs, seconds, too_many = asyncio.run(stt_diarize.diarize_chunks(
        [{"path": "a.mp3", "start_time": 0}, {"path": "b.mp3", "start_time": 300}], "k", "ru", tmp_path))
    assert [s["speaker"] for s in segs] == [1, 2, 2, 1, 3]
    assert segs[2]["start"] == 300 and seconds == 600 and not too_many
    assert [name for name, _ in calls[1]] == ["spk1", "spk2"]


def test_srt_speaker_tags_read_and_written():
    srt = ("1\n00:00:01,000 --> 00:00:02,000 [spk:1] [speed:fast]\nSalom.\n\n"
           "2\n00:00:02,500 --> 00:00:03,000 [spk:2]\nHa.\n")
    segs = translation.parse_srt_direct(srt)
    assert [s.get("speaker") for s in segs] == [1, 2]
    learning = translation.parse_learning_srt(srt.replace("[speed:fast]", "[yangi:зуб=tish]"))
    assert learning[0]["speaker"] == 1 and learning[0]["words"][0]["lemma"] == "зуб"
    out = worker.transcription.build_srt(segs)
    assert "--> 00:00:02,000 [spk:1]" in out
    assert "[spk:" not in worker.transcription.build_srt([{"start": 0, "end": 1, "text": "x", "speaker": 1}])


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


def test_voice_per_speaker_and_cache_key(client, monkeypatch, tmp_path):
    blocks = [{"start": 0, "end": 2, "text": "Savol bormi?", "speaker": 1},
              {"start": 2, "end": 4, "text": "Ha, bor.", "speaker": 2},
              {"start": 4, "end": 6, "text": "Yaxshi.", "speaker": 3}]
    job_id = tts.create_job("t", "openai", blocks, voice="alloy",
                            voice_map={"1": {"voice": "onyx"}, "2": {"voice": "nova"}})
    tts.TTS_QUEUE.get_nowait()
    job = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    assert [tts.voice_for_speaker(job, s)[0] for s in (1, 2, 3, None)] == ["onyx", "nova", "alloy", "alloy"]
    used = []

    async def fake_tts(client_, text, voice, api_key, instructions, speed=1.0):
        used.append((text, voice, speed))
        return b"RIFF"

    monkeypatch.setattr(tts, "openai_tts_generate_one", fake_tts)
    monkeypatch.setattr(tts.keys_manager, "get_next_active_key", lambda **k: ("kid", "sk"))
    monkeypatch.setattr(tts.keys_manager, "mark_result", lambda *a, **k: None)
    segs = db.fetchall("SELECT * FROM tts_segments WHERE job_id = ? ORDER BY seg_index", (job_id,))
    ctx = {"stop": False}

    async def run():
        lock = asyncio.Lock()
        for seg in segs:
            await tts._process_segment(None, job, seg, lock, ctx, tmp_path)

    asyncio.run(run())
    assert [(t, v) for t, v, _ in used] == [("Savol bormi?", "onyx"), ("Ha, bor.", "nova"), ("Yaxshi.", "alloy")]
    assert all(speed == 1.0 for *_, speed in used)
    keys = {r["cache_key"] for r in db.fetchall("SELECT cache_key FROM tts_segments WHERE job_id = ?", (job_id,))}
    assert len(keys) == 3
    assert tts.cache_key_for("openai", "x", voice="onyx", instructions="", speed=1.0) != \
        tts.cache_key_for("openai", "x", voice="nova", instructions="", speed=1.0)


def test_speakers_endpoint_and_reassign(client):
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    vid = db.new_id()
    originals = [{"start": 0, "end": 2, "text": "Вопрос?", "speaker": 1},
                 {"start": 2, "end": 4, "text": "Ответ.", "speaker": 2}]
    translations = [{"source_indices": [0], "start": 0, "end": 2, "text": "Savol?", "speaker": 1},
                    {"source_indices": [1], "start": 2, "end": 4, "text": "Javob.", "speaker": 2}]
    db.execute("INSERT INTO videos (id, original_name, status, owner_id, transcript_segments, translation_segments, "
               "created_at, updated_at) VALUES (?, 'v.mp4', 'translation_ready', ?, ?, ?, ?, ?)",
               (vid, owner, json.dumps(originals), json.dumps(translations), db.now(), db.now()))
    r = client.post(f"/api/videos/{vid}/transcript/speaker-names", json={"names": {"1": "Lektor"}})
    assert r.json()["names"] == {"1": "Lektor"}
    sp = client.get(f"/api/videos/{vid}/speakers").json()["speakers"]
    assert [(s["speaker"], s["name"], s["sentences"]) for s in sp] == [(1, "Lektor", 1), (2, "", 1)]
    r = client.post(f"/api/videos/{vid}/transcript/segments/1/speaker", data={"speaker": "1"})
    assert r.json()["translation_blocks_changed"] == 1
    v = db.fetchone("SELECT translation_segments FROM videos WHERE id = ?", (vid,))
    assert json.loads(v["translation_segments"])[1]["speaker"] == 1
    assert client.post(f"/api/videos/{vid}/audio", data={"provider": "openai", "voice_map": "[1]"}).status_code == 400
