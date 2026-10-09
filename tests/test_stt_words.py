"""So'z vaqtlaridan bloklar, uydirma filtri, ElevenLabs Scribe va SRT import (2.6, 2.7, 2.10)."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database as db
import keys_manager
import stt_elevenlabs
import stt_words
import transcription as T
import worker
from storage import VIDEOS_DIR


def W(text, s, e, spk=None):
    return {"w": text, "s": s, "e": e, "spk": spk}


def test_blocks_split_on_pause_and_sentence_end():
    words = [W("Привет,", 0.0, 0.4), W("друзья.", 0.45, 1.2), W("Сегодня", 1.25, 1.7), W("зубы", 2.4, 2.8),
             W("и", 2.85, 2.9), W("дёсны.", 2.95, 3.5)]
    blocks = stt_words.build_segments_from_words(words)
    assert [b["text"] for b in blocks] == ["Привет, друзья.", "Сегодня", "зубы и дёсны."]
    assert blocks[0]["start"] == 0.0 and blocks[0]["end"] == 1.2


def test_block_limits_and_no_zero_length():
    words = [W(f"слово{i},", i * 0.5, i * 0.5 + 0.45) for i in range(40)]
    blocks = stt_words.build_segments_from_words(words)
    assert all(b["end"] - b["start"] <= 7.0 for b in blocks)
    assert all(len(b["text"]) <= 90 for b in blocks)
    assert " ".join(b["text"] for b in blocks).split() == [w["w"] for w in words]
    one = stt_words.build_segments_from_words([W("Да.", 5.0, 5.0)])
    assert one[0]["end"] - one[0]["start"] >= 0.299


def test_speaker_change_starts_new_block():
    blocks = stt_words.build_segments_from_words([W("Вопрос", 0, 0.5, 1), W("ответ", 0.55, 1.0, 2)])
    assert [(b["text"], b["speaker"]) for b in blocks] == [("Вопрос", 1), ("ответ", 2)]


def test_hallucinations_removed_and_restorable():
    blocks = [{"start": 0, "end": 2, "text": "Начнём лекцию."},
              {"start": 30, "end": 32, "text": "Подпишись на канал!"},
              {"start": 40, "end": 45, "text": "Не забудьте, подпишись на канал, если вам интересна тема ортодонтии и брекетов."}]
    words = [W("Подпишись", 30, 30.5), W("на", 30.6, 30.7), W("канал!", 30.8, 31.5), W("Начнём", 0, 0.5)]
    kept, kept_words, removed = stt_words.remove_hallucinations(blocks, words, ["Субтитры: ИП Иванов"])
    assert [b["start"] for b in kept] == [0, 40]
    assert len(removed) == 1 and removed[0]["status"] == "removed" and len(removed[0]["words"]) == 3
    assert [w["w"] for w in kept_words] == ["Начнём"]


def test_whisper_words_get_punctuation_and_no_speech_filtered():
    data = {
        "segments": [{"start": 0, "end": 2, "text": " Привет, мир.", "no_speech_prob": 0.01, "avg_logprob": -0.2},
                     {"start": 5, "end": 7, "text": " Спасибо за просмотр!", "no_speech_prob": 0.9,
                      "avg_logprob": -1.4}],
        "words": [{"word": "Привет", "start": 0.1, "end": 0.6}, {"word": "мир", "start": 0.7, "end": 1.1},
                  {"word": "Спасибо", "start": 5.0, "end": 5.5}, {"word": "за", "start": 5.6, "end": 5.7},
                  {"word": "просмотр", "start": 5.8, "end": 6.4}],
    }
    words, removed = stt_words.whisper_chunk_words(data, 300.0)
    assert [w["w"] for w in words] == ["Привет,", "мир."]
    assert words[0]["s"] == 300.1
    assert removed[0]["segment"]["text"] == "Спасибо за просмотр!"


def test_overlap_dedupe():
    a = [W("один", 0, 1), W("два", 290, 299.5)]
    b = [W("два", 298.1, 299.5), W("три", 300, 301)]
    assert [w["w"] for w in stt_words.dedupe_overlap([a, b])] == ["один", "два", "три"]


def test_chunk_cuts_at_longest_silence():
    ranges = T.plan_chunk_ranges(700, [(285, 286), (295, 298.0), (318, 319)], 300)
    assert ranges[0] == (0.0, 296.5)
    assert ranges[1][0] == 296.5


def test_elevenlabs_words_and_speakers():
    data = {"language_code": "rus", "words": [
        {"text": "Здравствуйте.", "start": 0.0, "end": 0.8, "type": "word", "speaker_id": "speaker_3"},
        {"text": " ", "start": 0.8, "end": 0.9, "type": "spacing", "speaker_id": "speaker_3"},
        {"text": "(смех)", "start": 0.9, "end": 1.5, "type": "audio_event", "speaker_id": "speaker_3"},
        {"text": "Добрый", "start": 2.0, "end": 2.4, "type": "word", "speaker_id": "speaker_0"},
        {"text": "день.", "start": 2.45, "end": 2.9, "type": "word", "speaker_id": "speaker_0", "logprob": -0.1}]}
    ids = {}
    words = stt_elevenlabs.response_words(data, 10.0, ids)
    assert [(w["w"], w["spk"], w["s"]) for w in words] == [("Здравствуйте.", 1, 10.0), ("Добрый", 2, 12.0),
                                                          ("день.", 2, 12.45)]


def test_elevenlabs_form_and_keyterms():
    terms = stt_elevenlabs.keyterms_for("ru")
    assert 0 < len(terms) <= 1000
    assert all(len(t) < 50 and len(t.split()) <= 5 and not set("<>{}[]\\") & set(t) for t in terms)
    form = stt_elevenlabs.build_form("ru", True, 3, terms[:5])
    assert form["model_id"] == "scribe_v2" and form["timestamps_granularity"] == "word"
    assert form["diarize"] == "true" and form["num_speakers"] == "3" and len(form["keyterms"]) == 5
    assert "num_speakers" not in stt_elevenlabs.build_form("ru", False, 3)


def test_elevenlabs_single_request_under_limits():
    assert stt_elevenlabs.plan_parts(9000, 2 * 1024 ** 3, []) == [(0.0, 9000)]
    parts = stt_elevenlabs.plan_parts(11 * 3600, 1024 ** 3, [(19790, 19810)])
    assert len(parts) == 2 and parts[0][1] == 19800.0


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


def _video(status="uploaded", path=None):
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    vid = db.new_id()
    db.execute("INSERT INTO videos (id, original_name, status, owner_id, path, duration, language, created_at, "
               "updated_at) VALUES (?, 'dars.mp4', ?, ?, ?, 60, 'ru', ?, ?)",
               (vid, status, owner, str(path) if path else None, db.now(), db.now()))
    return vid, owner


def test_srt_import_from_uploaded_with_speakers(client):
    vid, _ = _video()
    srt = ("1\n00:00:01,000 --> 00:00:03,000 [spk:1]\nНачнём.\n\n"
           "2\n00:00:04,000 --> 00:00:04,000 [spk:2]\nДа.\n\n"
           "3\n00:00:20,000 --> 00:00:22,000 [spk:1]\nПодпишись на канал\n")
    r = client.post(f"/api/videos/{vid}/transcript/srt-upload", files={"file": ("a.srt", srt.encode())},
                    data={"language": "ru"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segment_count"] == 2 and body["removed_count"] == 1 and body["fixed_zero"] == 1
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (vid,))
    segs = json.loads(v["transcript_segments"])
    assert segs[1]["speaker"] == 2 and segs[1]["end"] > segs[1]["start"]
    srt_out = Path(db.fetchone("SELECT path FROM results WHERE video_id = ? AND kind = 'srt'", (vid,))["path"])
    assert "[spk:2]" in srt_out.read_text(encoding="utf-8")
    r = client.post(f"/api/videos/{vid}/transcript/restore-removed", data={"issue_index": 0})
    assert r.status_code == 200 and r.json()["segment_count"] == 3


def test_elevenlabs_job_end_to_end(client, monkeypatch, tmp_path):
    src = VIDEOS_DIR / "el_src.mp4"
    subprocess.run([T.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=300:duration=3",
                    "-c:a", "aac", str(src)], check=True)
    vid, owner = _video(path=src)
    keys_manager.add_key("sk_test_elevenlabs_key", "el", "elevenlabs", owner_id=owner)
    sent = {}

    async def fake_transcribe(path, api_key, form, attempts=3):
        sent.update(form=form, path=path, key=api_key)
        return {"language_code": "rus", "words": [
            {"text": "Добрый", "start": 0.1, "end": 0.5, "type": "word", "speaker_id": "a"},
            {"text": "день.", "start": 0.55, "end": 1.0, "type": "word", "speaker_id": "a"},
            {"text": "Привет.", "start": 1.6, "end": 2.2, "type": "word", "speaker_id": "b"}]}

    monkeypatch.setattr(stt_elevenlabs, "transcribe", fake_transcribe)
    r = client.post(f"/api/videos/{vid}/transcribe", data={"language": "ru", "stt_provider": "elevenlabs",
                                                           "diarize": "true", "send_keyterms": "false"})
    assert r.status_code == 200, r.text
    asyncio.run(worker.run_transcription_job(vid))
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (vid,))
    assert v["status"] == "transcription_ready", v["error"]
    assert sent["path"].suffix == ".m4a" and sent["key"] == "sk_test_elevenlabs_key"
    assert "keyterms" not in sent["form"] and sent["form"]["diarize"] == "true"
    segs = json.loads(v["transcript_segments"])
    assert [(s["text"], s.get("speaker")) for s in segs] == [("Добрый день.", 1), ("Привет.", 2)]
    assert len(json.loads(v["transcript_words"])) == 3
    cost = db.fetchone("SELECT * FROM costs WHERE video_id = ? AND kind = 'stt_elevenlabs'", (vid,))
    assert cost and cost["amount_usd"] == pytest.approx(60 / 3600 * 0.22, abs=1e-6)


def test_openai_requires_chunks(client):
    vid, owner = _video(status="segments_ready")
    keys_manager.add_key("sk-test-openai", "o", "openai", owner_id=owner)
    r = client.post(f"/api/videos/{vid}/transcribe", data={"language": "ru", "stt_provider": "openai"})
    assert r.status_code == 400 and "bo'laklarga" in r.json()["detail"]


def test_openai_word_path_end_to_end(client, monkeypatch):
    vid, owner = _video(status="segments_ready")
    keys_manager.add_key("sk-test-openai-2", "o", "openai", owner_id=owner)
    for i, (start, end) in enumerate([(0.0, 30.0), (28.0, 60.0)]):
        db.execute("INSERT INTO chunks (id, video_id, chunk_index, start_time, end_time, path, status, created_at, "
                   "updated_at) VALUES (?, ?, ?, ?, ?, 'x.mp3', 'pending', ?, ?)",
                   (db.new_id(), vid, i, start, end, db.now(), db.now()))
    replies = {
        0.0: {"language": "russian", "text": "Начнём. Тема",
              "segments": [{"start": 0, "end": 29.5, "text": " Начнём. Тема", "no_speech_prob": 0.0, "avg_logprob": -0.1}],
              "words": [{"word": "Начнём", "start": 1.0, "end": 1.6}, {"word": "Тема", "start": 29.0, "end": 29.5}]},
        28.0: {"language": "russian", "text": "Тема зубы.",
               "segments": [{"start": 0, "end": 3, "text": " Тема зубы.", "no_speech_prob": 0.0, "avg_logprob": -0.1},
                            {"start": 20, "end": 22, "text": " Продолжение следует...", "no_speech_prob": 0.1,
                             "avg_logprob": -0.3}],
               "words": [{"word": "Тема", "start": 1.0, "end": 1.5}, {"word": "зубы", "start": 1.6, "end": 2.1},
                         {"word": "Продолжение", "start": 20, "end": 21}, {"word": "следует", "start": 21.1, "end": 21.8}]},
    }

    async def fake_api(client_, path, api_key, language, prompt):
        chunk = db.fetchone("SELECT start_time FROM chunks WHERE video_id = ? AND status = 'running'", (vid,))
        return replies[chunk["start_time"]]

    monkeypatch.setattr(T, "transcribe_chunk_via_api", fake_api)
    monkeypatch.setattr(worker, "MAX_WHISPER_CONCURRENCY", 1)
    worker.start_transcription(vid, "ru", "")
    worker.TRANSCRIBE_QUEUE.get_nowait()
    asyncio.run(worker.run_transcription_job(vid))
    v = db.fetchone("SELECT * FROM videos WHERE id = ?", (vid,))
    assert v["status"] == "transcription_ready"
    segs = json.loads(v["transcript_segments"])
    assert [s["text"] for s in segs] == ["Начнём.", "Тема зубы."]
    assert segs[1]["start"] == 29.0  # takroriy "Тема" (overlap) bir marta
    issues = json.loads(v["flagged_issues"])
    assert any(i["kind"] == "removed" and "Продолжение" in i["segment"]["text"] for i in issues)
