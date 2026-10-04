"""2-bosqich: ~1 daqiqalik video, 3 ta takror va 2 ta yangi so'z. TTS soxta
(sinus WAV), qolgani - haqiqiy ffmpeg, Pillow, baza va worker oqimi."""
import asyncio
import io
import json
import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

import database as db
import intro
import keys_manager
import learning
import transcription
import translation
import tts
import worker

SRT = """1
00:00:05,000 --> 00:00:09,000 [yangi:че́люсть=jag‘] [takror:зуб=tish]
Pastki челюсть va зуб.

2
00:00:12,000 --> 00:00:15,000 [takror:суста́в=bo‘g‘im]
Сустав haqida.

3
00:00:20,000 --> 00:00:24,000 [yangi:enamel=emal] [takror:десна=milk]
Enamel va десна.

4
00:00:30,000 --> 00:00:33,000
Tegsiz blok.
"""

ORIG_SEC, UZ_SEC = 0.7, 0.9


def _wav(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(int(seconds * rate))))
    return buf.getvalue()


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    db.init_db()
    work = tmp_path_factory.mktemp("intro")
    src = work / "learning.mp4"
    # Integratsiya uchun kichik kadr/fps yetarli; SRT'ning oxirgi 33-soniyali
    # cue'sini ham qamraydi va CI'da dasturiy x264 renderini ancha tezlashtiradi.
    subprocess.run([transcription.ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=320x180:rate=5", "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=24000",
                    "-t", "36", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(src)],
                   check=True)
    vid = db.new_id()
    db.execute("INSERT INTO videos (id, original_name, status, created_at, updated_at) VALUES (?, ?, 'completed', ?, ?)",
               (vid, "dars.mp4", db.now(), db.now()))
    blocks = translation.parse_learning_srt(SRT)
    db.execute("INSERT INTO learning_tracks (id, video_id, srt_filename, srt_status, segment_count, final_video_path, "
               "final_video_status, freeze_points, words_json, warnings_json, created_at, updated_at) "
               "VALUES (?, ?, 'Dars_LEARNING.srt', 'uploaded', 4, ?, 'ready', ?, ?, '[]', ?, ?)",
               (db.new_id(), vid, str(src), json.dumps([{"time": 10.0, "duration": 1.0}]),
                json.dumps(blocks, ensure_ascii=False), db.now(), db.now()))
    return vid, src


@pytest.fixture()
def fake_tts(monkeypatch):
    calls = {"openai": [], "aisha": []}

    async def openai(client, text, voice, api_key, instructions, speed=1.0):
        calls["openai"].append((text, instructions, speed))
        return _wav(UZ_SEC if instructions == intro.INSTRUCTIONS["uz"] else ORIG_SEC)

    async def aisha(client, text, mood, speed, api_key):
        calls["aisha"].append(text)
        return _wav(UZ_SEC, 22050)

    monkeypatch.setattr(tts, "openai_tts_generate_one", openai)
    monkeypatch.setattr(tts, "aisha_generate_one", aisha)
    monkeypatch.setattr(keys_manager, "get_next_active_key", lambda **kw: ("k1", "sk-test"))
    monkeypatch.setattr(keys_manager, "mark_result", lambda *a, **kw: None)
    return calls


def _run(vid):
    async def go():
        await worker.run_learning_intro(vid)
        await worker.run_learning_export(vid)
    asyncio.run(go())
    worker.LEARNING_EXTRA_QUEUE = asyncio.Queue()


def test_intro_end_to_end(setup, fake_tts):
    vid, src = setup
    assert worker.enqueue_learning_intro(vid)
    _run(vid)
    t = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (vid,))
    assert t["intro_status"] == "ready", t["intro_error"]
    assert t["export_status"] == "ready", t["export_error"]
    assert t["export_with_intro"] == 1

    # Original so'z ham, o'zbekcha ma'no ham OpenAI orqali avtomatik o'qiladi.
    assert [c[0] for c in fake_tts["openai"]] == ["че́люсть", "jag‘", "enamel", "emal"]
    assert fake_tts["openai"][0][1] == intro.INSTRUCTIONS["ru"]
    assert fake_tts["openai"][1][1] == intro.INSTRUCTIONS["uz"]
    assert fake_tts["openai"][2][1] == intro.INSTRUCTIONS["en"]
    assert fake_tts["openai"][3][1] == intro.INSTRUCTIONS["uz"]
    assert all(c[2] == 1.0 for c in fake_tts["openai"])
    assert fake_tts["aisha"] == []

    # 4.2-jadval: 2 + (3 + 1.2*3) + 2 + 2 * (0.5 + 0.7 + 0.6 + 0.9 + 0.6 + 0.7 + 1.2)
    expected = 2 + 6.6 + 2 + 2 * intro.card_seconds(ORIG_SEC, UZ_SEC)
    assert t["intro_duration"] == pytest.approx(expected, abs=0.05)
    slides = json.loads(t["intro_slides_json"])
    assert [s["kind"] for s in slides] == ["title", "repeat", "title", "card", "card"]

    # Intro aynan Learning videosi parametrlari bilan
    li, ii = learning.probe_media(src), learning.probe_media(Path(t["intro_video_path"]))
    assert learning.params_match(li, ii), (li, ii)

    # Intro bilan yig'ilgan video = intro + Learning
    export_dur = transcription.get_duration_seconds(Path(t["export_video_path"]))
    assert export_dur == pytest.approx(t["intro_duration"] + li["duration"], abs=0.2)

    # Surish: avval freeze-point (10s da +1s), keyin intro; intro paytida hech qanday cue yo'q
    blocks = worker.learning_blocks(t)
    cues = learning.words_cues(blocks, worker.learning_freeze_points(t), worker.learning_intro_offset(t))
    assert cues[0]["start"] == pytest.approx(t["intro_duration"] + 5.0, abs=0.001)
    assert cues[1]["start"] == pytest.approx(t["intro_duration"] + 13.0, abs=0.001)
    assert min(c["start"] for c in cues) >= t["intro_duration"]
    subs = learning.shifted_segments(translation.parse_srt_direct(SRT), worker.learning_freeze_points(t),
                                     worker.learning_intro_offset(t))
    assert subs[0]["start"] == pytest.approx(t["intro_duration"] + 5.0, abs=0.001)


def test_second_run_uses_audio_cache(setup, fake_tts):
    vid, _ = setup
    assert worker.enqueue_learning_intro(vid)
    _run(vid)
    t = db.fetchone("SELECT intro_status, intro_error FROM learning_tracks WHERE video_id = ?", (vid,))
    assert t["intro_status"] == "ready", t["intro_error"]
    assert fake_tts["openai"] == [] and fake_tts["aisha"] == []


def test_strip_stress_setting(setup, fake_tts):
    vid, _ = setup
    assert worker.enqueue_learning_intro(vid, strip_stress=True)
    _run(vid)
    assert [c[0] for c in fake_tts["openai"]] == ["челюсть"]  # enamel keshdan


def test_missing_intro_dependency_sets_error_instead_of_staying_queued(setup, monkeypatch):
    """Pillow/intro importi yiqilsa vazifa abadiy ``generating`` qolmasin."""
    import builtins

    vid, _ = setup
    assert worker.enqueue_learning_intro(vid)
    original_import = builtins.__import__

    def fail_intro_import(name, *args, **kwargs):
        if name == "intro":
            raise ModuleNotFoundError("No module named 'PIL'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_intro_import)
    asyncio.run(worker.run_learning_intro(vid))
    track = db.fetchone("SELECT intro_status, intro_message, intro_error FROM learning_tracks WHERE video_id = ?", (vid,))
    assert track["intro_status"] == "error"
    assert track["intro_message"] is None
    assert "No module named 'PIL'" in track["intro_error"]
    worker.LEARNING_EXTRA_QUEUE = asyncio.Queue()
