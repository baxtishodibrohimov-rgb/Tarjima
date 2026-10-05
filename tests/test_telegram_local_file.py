from pathlib import Path

import telegram_bot


def test_copy_local_bot_file_copies_absolute_file(tmp_path):
    source = tmp_path / "bot-cache.mp4"
    source.write_bytes(b"large-video-placeholder")
    destination = tmp_path / "cloud" / "video.mp4"
    destination.parent.mkdir()

    size = telegram_bot._copy_local_bot_file(str(source.resolve()), destination)

    assert size == len(b"large-video-placeholder")
    assert destination.read_bytes() == source.read_bytes()


def test_copy_local_bot_file_uses_http_fallback_for_relative_path(tmp_path):
    destination = tmp_path / "video.mp4"

    size = telegram_bot._copy_local_bot_file("relative/bot-cache.mp4", destination)

    assert size is None
    assert not destination.exists()


def test_large_get_file_timeout_is_two_hours():
    assert telegram_bot.GET_FILE_TIMEOUT == 7200
