"""Yagona vaqt funksiyasi (slow/freeze), render va pleyer VTT'lari (2.1, 2.2)."""
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import database as db
import learning
import transcription as T
from storage import RESULTS_DIR

LEGACY = [{"time": 5.0, "duration": 2.0}]
SLOW = [{"type": "slow", "start": 10.0, "end": 14.0, "extra": 2.0}]
MIXED = LEGACY + SLOW + [{"type": "freeze", "time": 14.0, "duration": 1.0},
                         {"type": "slow", "start": 14.0, "end": 16.0, "extra": 0.5}]


def test_legacy_freeze_format():
    assert T.source_time_to_final_time(4.99, LEGACY) == 4.99
    assert T.source_time_to_final_time(5.0, LEGACY) == 7.0
    assert T.source_time_to_final_time(20, LEGACY) == 22.0
    assert T.total_freeze_duration(LEGACY) == 2.0


def test_slow_interpolates_inside_interval():
    assert T.source_time_to_final_time(10.0, SLOW) == 10.0
    assert T.source_time_to_final_time(12.0, SLOW) == 13.0
    assert T.source_time_to_final_time(14.0, SLOW) == 16.0
    assert T.source_time_to_final_time(30.0, SLOW) == 32.0


def test_mixed_points_and_inverse():
    assert T.total_timeline_extra(MIXED) == 5.5
    for t in (0, 3, 5, 6.5, 10, 11.3, 13.99, 14, 15, 16, 40):
        f = T.source_time_to_final_time(t, MIXED)
        assert abs(T.final_time_to_source_time(f, MIXED) - t) < 0.002
    # freeze ichidagi yakuniy vaqt freeze nuqtasiga tushadi
    assert T.final_time_to_source_time(6.0, MIXED) == 5.0


def test_tiny_points_ignored_everywhere():
    points = [{"time": 3, "duration": 0.04}, {"type": "slow", "start": 1, "end": 2, "extra": 0.001}]
    assert T.active_timeline_points(points) == []
    assert T.source_time_to_final_time(10, points) == 10
    assert learning.shift_time(10, points) == 10


def test_subtitle_writers_use_same_function():
    segs = [{"start": 11.0, "end": 15.0, "text": "a"}]
    out = T.apply_freeze_to_segments(segs, MIXED)[0]
    assert out["start"] == T.source_time_to_final_time(11.0, MIXED)
    assert out["end"] == T.source_time_to_final_time(15.0, MIXED)
    assert learning.shift_time(11.0, MIXED, 3.0) == round(out["start"] + 3.0, 3)
    assert segs[0]["start"] == 11.0  # manba o'zgarmaydi


def test_timeline_message():
    assert T.timeline_message([]) == ""
    msg = T.timeline_message(MIXED)
    assert "2 ta joyda video sekinlashtirildi (jami 2.5 s)" in msg
    assert "2 ta joyda kutish" in msg


def test_filter_groups_keep_offsets_consistent():
    points = [{"type": "slow", "start": i, "end": i + 0.5, "extra": 0.1} for i in range(100)]
    vf = T.build_timeline_video_filter(points, 25)
    assert vf.count("setpts=(T+") == 3  # 40 + 40 + 20
    assert vf.endswith("fps=25,format=yuv420p")


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe kerak")
def test_render_slows_video_and_keeps_length(tmp_path):
    ff = T.ffmpeg_exe()
    src, audio, out = tmp_path / "src.mp4", tmp_path / "a.wav", tmp_path / "out.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=300:duration=9", str(audio)],
                   check=True)
    points = [{"type": "slow", "start": 1.0, "end": 3.0, "extra": 0.6}, {"time": 4.0, "duration": 0.8},
              {"time": 5.99, "duration": 0.5}]
    target = 6.0 + T.total_timeline_extra(points)
    T.mux_video_audio_with_freezes(src, audio, out, points, tmp_path / "w", target)
    got = T.get_duration_seconds(out)
    assert abs(got - target) < 0.1
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                            "-show_entries", "stream=nb_read_frames,width,r_frame_rate", "-of", "csv=p=0",
                            str(out)], capture_output=True, text=True).stdout.strip()
    width, rate, frames = probe.split(",")
    assert width == "160" and rate == "25/1"
    assert abs(int(frames) - round(target * 25)) <= 2


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).status_code == 200
        yield c


def test_vtt_endpoints_follow_selected_timeline(client):
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    vid = db.new_id()
    db.execute("INSERT INTO videos (id, original_name, status, owner_id, freeze_points, created_at, updated_at) "
               "VALUES (?, 'v.mp4', 'completed', ?, ?, ?, ?)",
               (vid, owner, '[{"type": "slow", "start": 0, "end": 10, "extra": 5}]', db.now(), db.now()))
    db.execute("INSERT INTO audio_tracks (id, video_id, provider, freeze_points) VALUES (?, ?, 'openai', ?)",
               (db.new_id(), vid, '[{"time": 1, "duration": 3}]'))
    d = RESULTS_DIR / vid
    d.mkdir(parents=True)
    vtt = "WEBVTT\n\n1\n00:00:02.000 --> 00:00:04.000\nsalom\n"
    for kind in ("vtt_original", "vtt_uz"):
        (d / f"{kind}.vtt").write_text(vtt, encoding="utf-8")
        db.execute("INSERT INTO results (id, video_id, kind, filename, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (db.new_id(), vid, kind, f"{kind}.vtt", str(d / f"{kind}.vtt"), db.now()))

    src = client.get(f"/api/videos/{vid}/subtitles/original.vtt?timeline=source").text
    assert "00:00:02.000 --> 00:00:04.000" in src
    fin = client.get(f"/api/videos/{vid}/subtitles/original.vtt?timeline=final").text
    assert "00:00:03.000 --> 00:00:06.000" in fin
    uz = client.get(f"/api/videos/{vid}/subtitles/uz.vtt?timeline=final:openai").text
    assert "00:00:05.000 --> 00:00:07.000" in uz
    assert "salom" in uz
    assert client.get(f"/api/videos/{vid}/subtitles/uz.vtt?timeline=bad").status_code == 400

    detail = client.get(f"/api/videos/{vid}").json()
    assert detail["timeline_points"][0]["type"] == "slow"
    assert detail["track_timeline_points"]["openai"][0]["type"] == "freeze"
