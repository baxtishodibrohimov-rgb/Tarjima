"""Gap birligida ovoz joylash, tempo va pitch (2.3, 2.4, 2.5)."""
import array
import math
import subprocess
import wave
from pathlib import Path

import pytest

import database as db
import transcription as T
import tts
import tts_plan
from timing_contract import SLOWMO_MIN_RATE, TEMPO_MAX, TEMPO_MIN, TEMPO_STEP


def blk(start, end, text, speaker=None):
    return {"start": start, "end": end, "text": text, **({"speaker": speaker} if speaker else {})}


def test_units_group_blocks_into_sentences():
    blocks = [blk(0, 2, "Bugun biz"), blk(2, 4, "tishlarni ko'ramiz."), blk(5, 6, 'U dedi: "Ha."'),
              blk(7, 8, "Savol bor (yo'qmi?)"), blk(9, 10, ""), blk(11, 12, "Oxirgi")]
    units = tts_plan.build_units(blocks, "openai")
    texts = [(u["text"], u["block_start"], u["block_end"], u["skipped"]) for u in units]
    assert texts == [
        ("Bugun biz tishlarni ko'ramiz.", 0, 1, False),
        ('U dedi: "Ha."', 2, 2, False),
        ("Savol bor (yo'qmi?)", 3, 3, False),
        ("", 4, 4, True),
        ("Oxirgi", 5, 5, False),
    ]
    assert units[0]["start"] == 0 and units[0]["end"] == 4


def test_speaker_change_starts_new_sentence():
    units = tts_plan.build_units([blk(0, 1, "Salom", 1), blk(1, 2, "dunyo", 2)], "openai")
    assert [(u["text"], u["speaker"]) for u in units] == [("Salom", 1), ("dunyo", 2)]


def test_long_sentence_split_at_commas_within_limit():
    text = ", ".join(["so'z " * 30] * 12).strip() + "."
    units = tts_plan.build_units([blk(0, 60, text)], "aisha")
    assert len(units) > 1
    assert all(len(u["text"]) <= 1000 for u in units)
    assert {u["sentence_index"] for u in units} == {0}
    assert [u["part_index"] for u in units] == list(range(len(units)))
    assert all(u["text"].endswith(",") for u in units[:-1])
    assert " ".join(u["text"] for u in units).split() == text.split()  # matn o'zgarmaydi


def test_legacy_units_grouped_by_punctuation():
    segs = [{"seg_index": i, "start_sec": float(i), "end_sec": i + 1.0, "text": t, "status": st,
             "audio_path": "x" if st == "completed" else None}
            for i, (t, st) in enumerate([("Bir", "completed"), ("ikki.", "completed"), ("", "skipped"),
                                         ("Uch.", "completed")])]
    sentences = tts_plan.group_sentences(segs)
    assert [[u["seg_index"] for u in s["units"]] for s in sentences] == [[0, 1], [2], [3]]
    assert [s["voiced"] for s in sentences] == [True, False, True]


def test_tempo_bounds_and_smoothing():
    n = 40
    durations = [3.0] * n
    available = [3.0] * n
    # sur'at 20-gapdan boshlab 20% oshadi
    paces = [14.0] * 20 + [16.8] * 20
    tempos, base = tts_plan.compute_tempos(durations, available, paces, [None] * n)
    assert base == 1.0
    assert all(TEMPO_MIN <= t <= TEMPO_MAX for t in tempos)
    assert all(abs(a - b) <= TEMPO_STEP + 1e-9 for a, b in zip(tempos, tempos[1:]))
    assert tempos[0] < tempos[-1]
    assert tempos[-1] == pytest.approx(min(16.8 / 15.4, TEMPO_MAX), abs=0.01)
    # silliq oshish: bir necha gap davomida
    jumps = [b - a for a, b in zip(tempos, tempos[1:]) if b - a > 1e-6]
    assert len(jumps) >= 3


def test_tempo_pace_ref_per_speaker():
    paces = [10, 20, 10, 20, 10, 20]
    speakers = [1, 2, 1, 2, 1, 2]
    tempos, _ = tts_plan.compute_tempos([2] * 6, [3] * 6, paces, speakers)
    assert len(set(tempos)) == 1  # har kim o'z sur'atida - bir xil tempo


def test_base_tempo_for_fast_speaker():
    tempos, base = tts_plan.compute_tempos([4.0] * 5, [3.0] * 5, [None] * 5, [None] * 5)
    assert base == 1.10


def test_window_pace_uses_short_pauses_only():
    items = [(0.0, 1.0, 14, None), (1.2, 2.2, 14, None), (5.0, 6.0, 14, None)]
    pace = tts_plan.window_pace(items, [i[0] for i in items], 3.0)
    assert pace == pytest.approx(42 / 3.2)


def test_plan_points_slow_then_freeze():
    starts = [0.0, 10.0, 12.0, 20.0]
    available = tts_plan.available_times(starts, 30.0)
    assert available == [10.0, 2.0, 8.0, 10.0]
    needs = [9.0, 2.4, 20.0, 5.0]
    points = tts_plan.plan_points(starts, available, needs)
    assert points[0] == {"type": "slow", "start": 10.0, "end": 12.0, "extra": 0.4}
    slow, freeze = points[1], points[2]
    assert slow["type"] == "slow" and slow["extra"] == pytest.approx(8 / SLOWMO_MIN_RATE - 8, abs=0.001)
    assert freeze == {"type": "freeze", "time": 20.0, "duration": pytest.approx(12 - slow["extra"], abs=0.002)}
    # har gap ovozi keyingi gap boshigacha aniq sig'adi
    for i in range(3):
        span = T.source_time_to_final_time(starts[i + 1], points) - T.source_time_to_final_time(starts[i], points)
        assert span >= max(needs[i], available[i]) - 0.002


def _tone(path: Path, seconds: float, freq: float = 220.0, rate: int = 24000, lead=0.2, tail=0.4):
    samples = array.array("h")
    samples.extend([0] * int(lead * rate))
    samples.extend(int(8000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(seconds * rate)))
    samples.extend([0] * int(tail * rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


def _decode(path: Path, rate=24000):
    out = subprocess.run([T.ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                          "-ar", str(rate), "pipe:1"], capture_output=True).stdout
    s = array.array("h")
    s.frombytes(out[:len(out) - len(out) % 2])
    return s


def _onsets(samples, rate=24000, threshold=1500):
    win = rate // 100
    loud = [max(abs(x) for x in samples[i:i + win]) > threshold for i in range(0, len(samples) - win, win)]
    return [i / 100 for i in range(1, len(loud)) if loud[i] and not loud[i - 1]] + ([0.0] if loud and loud[0] else [])


def test_merge_places_each_sentence_at_its_start(tmp_path):
    # 3 gap: 1-si sig'adi, 2-si sig'maydi (video sekinlashadi), 3-si oxirgi.
    specs = [(0.5, 2.0), (4.0, 2.4), (6.0, 1.5)]
    segs = []
    for i, (start, dur) in enumerate(specs):
        path = tmp_path / f"u{i}.wav"
        _tone(path, dur, freq=220 + 110 * i)
        segs.append({"id": f"s{i}", "seg_index": i, "sentence_index": i, "part_index": 0, "start_sec": start,
                     "end_sec": start + 1, "text": f"Gap {i}.", "status": "completed", "audio_path": str(path),
                     "speaker": None})
    out = tmp_path / "out.mp3"
    points, stats = tts.merge_sentences(segs, out, True, video_duration=10.0)
    assert stats["sentences"] == 3
    assert stats["freeze_count"] == 0 and stats["slow_count"] >= 1
    assert all(TEMPO_MIN <= t <= TEMPO_MAX for t in (stats["tempo_min"], stats["tempo_max"]))
    onsets = _onsets(_decode(out))
    for start, _ in specs:
        expected = T.source_time_to_final_time(start, points)
        assert min(abs(o - expected) for o in onsets) <= 0.1, (expected, onsets)


def test_tempo_keeps_pitch():
    rate = 24000
    pcm = array.array("h", (int(8000 * math.sin(2 * math.pi * 300 * i / rate)) for i in range(rate * 2))).tobytes()
    out = tts._apply_tempo(pcm, 1, rate, 1.12)
    s = array.array("h")
    s.frombytes(out)
    assert len(s) / rate == pytest.approx(2 / 1.12, abs=0.08)
    mid = s[len(s) // 4: len(s) * 3 // 4]
    crossings = sum(1 for a, b in zip(mid, mid[1:]) if a < 0 <= b)
    assert crossings / (len(mid) / rate) == pytest.approx(300, rel=0.03)


@pytest.fixture(autouse=True, scope="module")
def _db():
    db.init_db()


def _job(units_blocks, provider="openai"):
    job_id = tts.create_job("t", provider, units_blocks)
    tts.TTS_QUEUE.get_nowait()
    return job_id


def test_rebuild_reuses_unchanged_sentence_audio():
    blocks = [blk(0, 1, "Bir"), blk(1, 2, "ikki."), blk(3, 4, "Uch.")]
    job_id = _job(blocks)
    rows = db.fetchall("SELECT * FROM tts_segments WHERE job_id = ? ORDER BY seg_index", (job_id,))
    assert [r["text"] for r in rows] == ["Bir ikki.", "Uch."]
    for r in rows:
        db.execute("UPDATE tts_segments SET status = 'completed', audio_path = ? WHERE id = ?",
                   (f"/x/{r['seg_index']}.wav", r["id"]))
    blocks[0]["text"] = "Birinchi"
    assert tts.rebuild_job_units(job_id, blocks, [0]) == 1
    rows = db.fetchall("SELECT * FROM tts_segments WHERE job_id = ? ORDER BY seg_index", (job_id,))
    assert [(r["text"], r["status"]) for r in rows] == [("Birinchi ikki.", "pending"), ("Uch.", "completed")]
    status = tts.block_audio_status(job_id)
    assert status[0]["status"] == status[1]["status"] == "pending" and status[2]["status"] == "completed"


def test_legacy_job_regenerates_only_changed_block():
    job_id = db.new_id()
    db.execute("INSERT INTO tts_jobs (id, title, provider, created_at) VALUES (?, 't', 'openai', ?)", (job_id, db.now()))
    for i, t in enumerate(["Bir", "ikki.", "Uch."]):
        db.execute("INSERT INTO tts_segments (id, job_id, seg_index, start_sec, end_sec, text, status, audio_path) "
                   "VALUES (?, ?, ?, ?, ?, ?, 'completed', ?)", (db.new_id(), job_id, i, i, i + 1, t, f"/x/{i}"))
    assert tts.is_legacy_job(job_id)
    blocks = [blk(0, 1, "Bir"), blk(1, 2, "ikkinchi."), blk(2, 3, "Uch.")]
    assert tts.rebuild_job_units(job_id, blocks, [1]) == 1
    rows = db.fetchall("SELECT text, status FROM tts_segments WHERE job_id = ? ORDER BY seg_index", (job_id,))
    assert [(r["text"], r["status"]) for r in rows] == [("Bir", "completed"), ("ikkinchi.", "pending"),
                                                        ("Uch.", "completed")]
