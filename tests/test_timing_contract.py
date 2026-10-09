import learning
import timing_contract as tc
import transcription as t


def test_time_formats_never_show_1000_ms():
    assert t.fmt_srt_time(1.9996) == "00:00:02,000"
    assert t.fmt_vtt_time(1.9996) == "00:00:02.000"
    assert t.fmt_srt_time(3599.9999) == "01:00:00,000"
    assert t.fmt_srt_time(0) == "00:00:00,000"
    assert t.fmt_srt_time(-0.2) == "00:00:00,000"
    assert t.fmt_srt_time(6089.123) == "01:41:29,123"
    assert learning._ass_time(1.996) == "0:00:02.00"


def test_sentence_end_detection():
    assert tc.ends_sentence("Bu gap tugadi.")
    assert tc.ends_sentence('U aytdi: "Bas."')
    assert tc.ends_sentence("(misol uchun tish.)")
    assert tc.ends_sentence("Rostmi?") and tc.ends_sentence("Ajoyib!") and tc.ends_sentence("Va hokazo…")
    assert tc.ends_sentence("Tamom. [spk:2]")
    assert not tc.ends_sentence("bu gap davom etadi,")
    assert not tc.ends_sentence("")
