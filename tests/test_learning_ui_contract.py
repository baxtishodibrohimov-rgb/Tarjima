from pathlib import Path


INDEX = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")


def _function_body(name: str, next_marker: str) -> str:
    start = INDEX.index(f"function {name}")
    return INDEX[start:INDEX.index(next_marker, start)]


def test_learning_audio_form_renders_before_background_requests():
    body = _function_body("showLearningAudioForm", "/* Tejash:")
    assert body.index("el.innerHTML = `") < body.index("apiFetch('/api/settings')")
    assert "Sozlamalar, spiker ovozlari va xarajat fonda yuklanmoqda" in body
    assert "learningAudioFormIsCurrent" in body
    assert "session.dirty" in INDEX
    assert "Promise.allSettled" in body


def test_learning_audio_submit_has_immediate_and_server_states():
    body = _function_body("submitLearningAudioJob", "function renderAudioTracksPanel")
    assert "So‘rov yuborilmoqda…" in body
    assert "btn.disabled = true" in body
    assert "Navbatda" in body
    assert "btn.disabled = false" in body
    assert "Audio so‘rovini yuborib bo‘lmadi" in body
    assert "Yaratilmoqda" in INDEX


def test_video_detail_uses_loading_placeholder_and_generation_guard():
    open_body = _function_body("openVideoDetail", "function closeVideoDetail")
    load_body = INDEX[INDEX.index("async function loadVideoDetail"):INDEX.index("async function pollVideoDetail")]
    assert "Yuklanmoqda…" in open_body
    assert "currentDetailLoadGeneration += 1" in open_body
    assert "currentDetailLoadGeneration !== requestGeneration" in load_body
    assert "Qayta urinish" in load_body


def test_learning_error_retry_and_process_log_reasons_are_visible():
    assert "Faqat xatoli bo‘laklarni qayta urinish" in INDEX
    assert "Learning audio: ${learningTrack.error_segment_count} ta segment xato" in INDEX
    assert "yakuniy muvaffaqiyatsiz sikl" in INDEX
    assert "oxirgi sikldagi provider urinishlari" in INDEX
