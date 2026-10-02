import pytest
from fastapi.testclient import TestClient

import app as app_module
import database as db

TAGGED = """1
00:00:01,000 --> 00:00:03,000 [yangi:че́люсть=jag‘] [takror:суста́в=bo‘g‘im]
Pastki челюстьning суставi.

2
00:00:04,000 --> 00:00:06,000 [speed:fast]
Tegsiz blok.
"""

UZBEK = """1
00:00:01,000 --> 00:00:03,000 [speed:slow]
Birinchi gap.

2
00:00:04,000 --> 00:00:06,000
Ikkinchi gap.
"""


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
        assert r.status_code == 200
        yield c


@pytest.fixture()
def video_id(client):
    owner = db.fetchone("SELECT id FROM users WHERE username = 'admin'")["id"]
    vid = db.new_id()
    db.execute("INSERT INTO videos (id, original_name, status, transcript_approved, owner_id, created_at, "
               "updated_at) VALUES (?, ?, 'transcription_approved', 1, ?, ?, ?)",
               (vid, "dars.mp4", owner, db.now(), db.now()))
    return vid


def _upload(client, vid, text, name="Dars_01_LEARNING.srt"):
    return client.post(f"/api/videos/{vid}/learning/srt", files={"file": (name, text.encode("utf-8"))})


def test_upload_parses_words_and_serves_track(client, video_id):
    r = _upload(client, video_id, TAGGED)
    assert r.status_code == 200, r.text
    words = client.get(f"/api/videos/{video_id}/learning/words").json()
    assert words["new_count"] == 1 and words["repeat_count"] == 1 and words["tagged_blocks"] == 1
    assert words["warnings"] == []
    vtt = client.get(f"/api/videos/{video_id}/learning/words.vtt")
    assert vtt.status_code == 200 and vtt.headers["content-type"].startswith("text/vtt")
    assert "00:00:01.000 --> 00:00:03.000 line:5% position:95% align:end" in vtt.text
    detail = client.get(f"/api/videos/{video_id}").json()
    assert detail["learning_words"]["new"][0]["lemma"] == "че́люсть"
    assert detail["learning_words"]["new"][0]["occurrences"] == 1
    assert detail["learning_words"]["repeat"][0]["occurrences"] == 1
    subs = client.get(f"/api/videos/{video_id}/learning/subtitles.vtt").text
    assert "Pastki челюстьning суставi." in subs and "[" not in subs


def test_download_names_follow_asos_rule(client, video_id):
    _upload(client, video_id, TAGGED)
    r = client.get(f"/api/videos/{video_id}/learning/words.vtt?download=1")
    assert "filename*=UTF-8''Dars_01_sozlar.vtt" in r.headers["content-disposition"]
    srt = client.get(f"/api/videos/{video_id}/learning/srt-download")
    assert 'filename="Dars_01_LEARNING.srt"' in srt.headers["content-disposition"]


def test_broken_tag_rejects_upload_and_keeps_previous(client, video_id):
    _upload(client, video_id, TAGGED)
    broken = TAGGED.replace("[takror:суста́в=bo‘g‘im]", "[takror:сустав]")
    r = _upload(client, video_id, broken, "boshqa.srt")
    assert r.status_code == 400
    assert r.json()["detail"].startswith("1-blok")
    track = db.fetchone("SELECT srt_filename FROM learning_tracks WHERE video_id = ?", (video_id,))
    assert track["srt_filename"] == "Dars_01_LEARNING.srt"


def test_warnings_are_saved(client, video_id):
    text = TAGGED.replace("Pastki челюстьning суставi.", "Boshqa matn.")
    assert _upload(client, video_id, text).status_code == 200
    warnings = client.get(f"/api/videos/{video_id}/learning/words").json()["warnings"]
    assert {"block": 1, "reason": "«че́люсть» so'zi blok matnida topilmadi."} in warnings


def test_block_editor_keeps_tags(client, video_id):
    _upload(client, video_id, TAGGED)
    r = client.post(f"/api/videos/{video_id}/learning/save-blocks",
                    json={"texts": ["Yangi челюсть суставi", "Ikkinchi"]})
    assert r.status_code == 200, r.text
    words = client.get(f"/api/videos/{video_id}/learning/words").json()
    assert words["new_count"] == 1 and words["warnings"] == []
    blocks = client.get(f"/api/videos/{video_id}/learning/blocks").json()
    assert [b["text"] for b in blocks] == ["Yangi челюсть суставi", "Ikkinchi"]


def test_intro_requires_learning_video(client, video_id):
    _upload(client, video_id, TAGGED)
    r = client.post(f"/api/videos/{video_id}/learning/intro")
    assert r.status_code == 400
    assert client.get(f"/api/videos/{video_id}/learning/words.vtt?intro=1").status_code == 404


def test_learning_subtitle_burn_uses_learning_provider(client, video_id, tmp_path, monkeypatch):
    _upload(client, video_id, TAGGED)
    final = tmp_path / "learning.mp4"
    final.write_bytes(b"video")
    db.execute("UPDATE learning_tracks SET final_video_status = 'ready', final_video_path = ? WHERE video_id = ?",
               (str(final), video_id))
    calls = []
    monkeypatch.setattr(app_module.worker, "enqueue_subtitle_burn",
                        lambda vid, provider=None: calls.append((vid, provider)) or True)
    r = client.post(f"/api/videos/{video_id}/subtitle-burn", data={"provider": "learning"})
    assert r.status_code == 200, r.text
    assert calls == [(video_id, "learning")]


def test_learning_words_require_embedded_export_for_user_download(client, video_id, tmp_path):
    _upload(client, video_id, TAGGED)
    clean = tmp_path / "clean-learning.mp4"
    clean.write_bytes(b"clean")
    db.execute("UPDATE learning_tracks SET final_video_status = 'ready', final_video_path = ?, "
               "export_status = 'generating' WHERE video_id = ?", (str(clean), video_id))

    r = client.get(f"/api/videos/{video_id}/learning/video-download")
    assert r.status_code == 409
    r = client.get(f"/api/videos/{video_id}/final-download?provider=learning")
    assert r.status_code == 409

    embedded = tmp_path / "words-embedded.mp4"
    embedded.write_bytes(b"words-embedded")
    db.execute("UPDATE learning_tracks SET export_status = 'ready', export_video_path = ? WHERE video_id = ?",
               (str(embedded), video_id))
    r = client.get(f"/api/videos/{video_id}/learning/video-download")
    assert r.status_code == 200 and r.content == b"words-embedded"


def test_uzbek_translation_srt_upload_unchanged(client, video_id):
    """Regressiya: o'zbekcha tayyor SRT yuklash va [speed:..] o'qilishi avvalgidek."""
    db.execute("UPDATE videos SET transcript_segments = ? WHERE id = ?",
               ('[{"start":1,"end":3,"text":"a"},{"start":4,"end":6,"text":"b"}]', video_id))
    r = client.post(f"/api/videos/{video_id}/translate/srt-direct",
                    files={"file": ("uz.srt", UZBEK.encode("utf-8"))})
    assert r.status_code == 200, r.text
    v = db.fetchone("SELECT translation_segments, translation_status FROM videos WHERE id = ?", (video_id,))
    assert v["translation_status"] == "uploaded"
    assert '"speed_tag": "slow"' in v["translation_segments"]
