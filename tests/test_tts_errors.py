import asyncio

import database as db
import httpx
import pytest
import tts

db.init_db()


def _job_and_segment(status="error"):
    job_id = db.new_id()
    seg_id = db.new_id()
    db.execute(
        "INSERT INTO tts_jobs (id, title, provider, voice, status, created_at) "
        "VALUES (?, 'Learning test', 'openai', 'alloy', ?, ?)",
        (job_id, status, db.now()),
    )
    db.execute(
        "INSERT INTO tts_segments (id, job_id, seg_index, start_sec, end_sec, text, status) "
        "VALUES (?, ?, 807, 91.25, 93.5, 'Test segment', 'pending')",
        (seg_id, job_id),
    )
    return job_id, seg_id


def test_empty_exception_message_still_records_type_stage_and_interval(tmp_path, monkeypatch):
    class EmptyProviderError(Exception):
        pass

    job_id, seg_id = _job_and_segment(status="running")
    job = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    seg = db.fetchone("SELECT * FROM tts_segments WHERE id = ?", (seg_id,))

    monkeypatch.setattr(tts.keys_manager, "get_next_active_key", lambda owner_id=None: ("key-id", "sk-secret"))
    monkeypatch.setattr(tts.keys_manager, "mark_result", lambda *args, **kwargs: None)

    async def fail(*args, **kwargs):
        raise EmptyProviderError()

    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(tts, "openai_tts_generate_one", fail)
    monkeypatch.setattr(tts.asyncio, "sleep", no_sleep)
    asyncio.run(tts._process_segment(object(), job, seg, asyncio.Lock(), {"stop": False}, tmp_path))

    saved = db.fetchone("SELECT * FROM tts_segments WHERE id = ?", (seg_id,))
    assert saved["status"] == "error"
    assert saved["attempts"] == 1  # bazadagi yakuniy muvaffaqiyatsiz sikl
    assert saved["last_api_attempts"] == 3  # shu sikldagi ichki provider urinishlari
    assert saved["error_stage"] == "openai_request"
    assert saved["error_type"] == "EmptyProviderError"
    assert "Segment 808 (indeks 807)" in saved["error"]
    assert "00:01:31.250–00:01:33.500" in saved["error"]
    assert "EmptyProviderError" in saved["error"]


def test_error_text_redacts_api_credentials():
    details = tts.segment_error_details(
        {"seg_index": 1, "start_sec": 0, "end_sec": 1},
        "openai_request",
        RuntimeError("Authorization: Bearer sk-very-secret-token"),
        1,
    )
    assert "sk-very-secret-token" not in details["message"]
    assert "[REDACTED]" in details["message"]


@pytest.mark.parametrize(("stage", "error", "category"), [
    ("openai_request", RuntimeError("insufficient_quota: billing credit tugadi"), "credit_or_quota"),
    ("openai_request", RuntimeError("HTTP 429 rate limit"), "rate_limit"),
    ("openai_request", asyncio.TimeoutError(), "timeout"),
    ("openai_request", httpx.ConnectError("connection failed"), "network"),
    ("segment_write", OSError("disk full"), "file_write"),
    ("audio_merge", RuntimeError("FFmpeg failed"), "ffmpeg"),
])
def test_error_categories_are_distinct_when_evidence_exists(stage, error, category):
    details = tts.segment_error_details(
        {"seg_index": 1, "start_sec": 0, "end_sec": 1}, stage, error, 1)
    assert details["category"] == category


def test_retry_only_requeues_errors_and_cannot_double_enqueue(monkeypatch):
    job_id, error_id = _job_and_segment(status="error")
    completed_id = db.new_id()
    db.execute(
        "INSERT INTO tts_segments (id, job_id, seg_index, start_sec, end_sec, text, status, audio_path) "
        "VALUES (?, ?, 1, 0, 1, 'Tayyor', 'completed', 'ready.wav')",
        (completed_id, job_id),
    )
    db.execute(
        "UPDATE tts_segments SET status = 'error', attempts = 2, error = 'old', "
        "error_stage = 'openai_request' WHERE id = ?",
        (error_id,),
    )

    class FakeQueue:
        def __init__(self):
            self.items = []

        def put_nowait(self, item):
            self.items.append(item)

    queue = FakeQueue()
    monkeypatch.setattr(tts, "TTS_QUEUE", queue)

    assert tts.retry_job(job_id) is True
    assert tts.retry_job(job_id) is False
    assert queue.items == [job_id]
    ready = db.fetchone("SELECT status, audio_path FROM tts_segments WHERE id = ?", (completed_id,))
    failed = db.fetchone("SELECT status, attempts, error FROM tts_segments WHERE id = ?", (error_id,))
    assert ready == {"status": "completed", "audio_path": "ready.wav"}
    assert failed["status"] == "pending" and failed["attempts"] == 2 and failed["error"] is None
