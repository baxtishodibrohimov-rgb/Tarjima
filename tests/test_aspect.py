"""Video nisbati saqlanishi: Telegram'ga o'lcham/thumbnail, Learning eksport (SAR, burilish)."""
import subprocess
from pathlib import Path

import pytest

import intro
import learning
import transcription as T


def _make(tmp_path, name, vf="", rotate=None):
    ff = T.ffmpeg_exe()
    base = tmp_path / f"{name}_base.mp4"
    size = "1440x1080" if "setsar" in vf else "640x360"
    cmd = [ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration=2",
           "-f", "lavfi", "-i", "sine=f=200:duration=2"]
    cmd += (["-vf", vf] if vf else []) + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
                                          str(base)]
    subprocess.run(cmd, check=True)
    if rotate is None:
        return base
    out = tmp_path / f"{name}.mp4"
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-display_rotation", str(rotate), "-i", str(base),
                        "-c", "copy", str(out)])
    if r.returncode != 0:
        pytest.skip("ffmpeg -display_rotation yo'q")
    return out


@pytest.mark.parametrize("kind,expected", [("plain", (640, 360)), ("sar", (1920, 1080)), ("rot", (360, 640))])
def test_display_size_and_telegram_fields(tmp_path, kind, expected):
    path = _make(tmp_path, kind, "setsar=4/3" if kind == "sar" else "", 90 if kind == "rot" else None)
    data, thumb = T.telegram_video_fields(path, tmp_path / "th")
    assert (int(data["width"]), int(data["height"])) == expected
    assert data["duration"] == "2"
    assert thumb and thumb.stat().st_size > 0


@pytest.mark.parametrize("kind", ["sar", "rot"])
def test_learning_export_keeps_shape_with_intro(tmp_path, kind):
    src = _make(tmp_path, kind, "setsar=4/3" if kind == "sar" else "", 90 if kind == "rot" else None)
    expected = T.video_display_info(src)
    info = learning.probe_media(src)
    assert info["normalize"] and (info["width"], info["height"]) == (expected["width"], expected["height"])
    intro_path = tmp_path / "intro.mp4"
    intro.build_intro_video([{"kind": "title", "text": "Sinov", "seconds": 1.0}], [], info, tmp_path / "iw",
                            intro_path, tmp_path / "slides")
    ass = learning.build_words_ass([{"start": 0.2, "end": 1.5,
                                     "words": [{"kind": "yangi", "lemma": "зуб", "meaning": "tish"}]}],
                                   info["width"], info["height"])
    out = tmp_path / "export.mp4"
    learning.build_export(src, out, tmp_path / "ew", info, ass, intro_path)
    got = T.video_display_info(out)
    assert (got["width"], got["height"]) == (expected["width"], expected["height"])


def test_ffmpeg_stopped_by_server_restart_is_not_an_error(tmp_path):
    import signal
    import time
    ff = T.ffmpeg_exe()
    proc = subprocess.Popen([ff, "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=600",
                             "-c:v", "libx264", "-preset", "ultrafast", str(tmp_path / "x.mp4")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate()
    with pytest.raises(T.FfmpegInterrupted):
        T.raise_if_interrupted(out, proc.returncode)
    T.raise_if_interrupted("Conversion failed!", 1)  # oddiy xato - FfmpegInterrupted emas


def test_learning_export_stays_in_progress_when_server_restarts(tmp_path, monkeypatch):
    import asyncio
    import json
    import database as db
    import worker
    db.init_db()
    src = _make(tmp_path, "lx")
    vid = db.new_id()
    db.execute("INSERT INTO videos (id, original_name, status, created_at, updated_at) VALUES (?, 'lx.mp4', "
               "'completed', ?, ?)", (vid, db.now(), db.now()))
    words = [{"index": 1, "start": 0.2, "end": 1.5, "text": "x", "words": [{"kind": "yangi", "lemma": "зуб",
                                                                           "meaning": "tish"}]}]
    db.execute("INSERT INTO learning_tracks (id, video_id, srt_status, final_video_status, final_video_path, "
               "export_status, words_json, created_at, updated_at) VALUES (?, ?, 'uploaded', 'ready', ?, "
               "'generating', ?, ?, ?)", (db.new_id(), vid, str(src), json.dumps(words), db.now(), db.now()))

    def stopped(*a, **k):
        raise T.FfmpegInterrupted("server restart")

    monkeypatch.setattr(learning, "build_export", stopped)
    asyncio.run(worker.run_learning_export(vid))
    track = db.fetchone("SELECT export_status, export_error FROM learning_tracks WHERE video_id = ?", (vid,))
    assert track["export_status"] == "generating" and not track["export_error"]
