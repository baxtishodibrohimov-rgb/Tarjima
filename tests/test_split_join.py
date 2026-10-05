import subprocess

import pytest

import transcription as t


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("video") / "sample.mp4"
    subprocess.run([t.ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", "25",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def video_packets(path):
    out = subprocess.run([t._ffprobe_exe(), "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


@pytest.mark.skipif(not t._ffprobe_exe(), reason="ffprobe kerak")
def test_split_join_split_roundtrip(sample_video, tmp_path):
    limit = sample_video.stat().st_size // 4
    parts = t.split_video_by_size(sample_video, tmp_path / "a", max_bytes=limit)
    assert len(parts) >= 4 and all(p.stat().st_size <= limit for p in parts)

    joined = tmp_path / "joined.mp4"
    t.join_video_parts(parts, joined, expected_duration=t.get_duration_seconds(sample_video))
    assert video_packets(joined) == video_packets(sample_video)
    assert not list(tmp_path.glob("*.joining*")) and not list(tmp_path.glob("*.parts.txt"))

    # Qayta yig'ilgan video ham xuddi asl kabi bo'linadi (vaqt siljishi kesishni buzmaydi).
    again = t.split_video_by_size(joined, tmp_path / "b", max_bytes=limit)
    assert [p.stat().st_size for p in again] == pytest.approx([p.stat().st_size for p in parts], rel=0.02)


def test_join_rejects_wrong_duration(sample_video, tmp_path):
    parts = t.split_video_by_size(sample_video, tmp_path / "a", max_bytes=sample_video.stat().st_size // 3)
    with pytest.raises(RuntimeError, match="davomiyligi"):
        t.join_video_parts(parts[:1], tmp_path / "out.mp4", expected_duration=20)
    assert not (tmp_path / "out.mp4").exists()
