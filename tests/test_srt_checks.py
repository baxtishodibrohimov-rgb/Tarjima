"""Tarjima va Learning SRT yuklashdagi ogohlantirishlar (2.11)."""
import srt_checks


def B(start, end, text, speaker=None):
    return {"start": start, "end": end, "text": text, **({"speaker": speaker} if speaker else {})}


def test_run_without_sentence_end_and_long_sentence():
    blocks = [B(i * 16, i * 16 + 15, f"qism {i}") for i in range(5)] + [B(90, 91, "Tamom.")]
    w = srt_checks.translation_warnings(blocks, [])
    reasons = " | ".join(x["reason"] for x in w)
    assert "4+ blok gap tugash belgisisiz" in reasons
    assert "60 s dan uzun" in reasons


def test_early_start_and_speaker_checks():
    originals = [B(10, 12, "Привет.", 1), B(20, 22, "Да.", 2)]
    blocks = [B(8.5, 12, "Salom."), B(20, 22, "Ha.")]
    w = srt_checks.translation_warnings(blocks, originals)
    reasons = [x["reason"] for x in w]
    assert any("nutqdan 1.5 s oldin" in r for r in reasons)
    assert any("[spk:N]" in r for r in reasons)
    mixed = srt_checks.translation_warnings([B(10, 11, "Bir", 1), B(11, 12, "ikki.", 2)], originals)
    assert any("spiker almashgan" in x["reason"] for x in mixed)


def test_estimated_slowdown_listed():
    text = "Juda uzun gap " * 10 + "."
    w = srt_checks.translation_warnings([B(0, 2, text), B(3, 5, "Keyingi.")], [], video_duration=10)
    assert any("sekinlashishi kerak" in x["reason"] and x["block"] == 1 for x in w)


def test_learning_must_match_uzbek_full():
    uz = [B(0, 2, "Salom.", 1), B(2, 4, "Qalay", 2)]
    learning = [B(0, 2, "Salom.", 1), B(2.2, 4, "Qalay.", 1)]
    w = srt_checks.learning_vs_uzbek_warnings(learning, uz)
    assert w == [{"block": 2, "time": 2.2,
                  "reason": "O'zbekcha tarjimadagi blokdan farq: vaqti, gap tugash belgisi, [spk] tegi."}]
    assert srt_checks.learning_vs_uzbek_warnings(uz, uz) == []
