import re

import pytest

import intro
import learning
import translation

SAMPLE = """1
00:00:10,000 --> 00:00:16,000 [yangi:че́люсть=jag‘] [takror:суста́в=bo‘g‘im]
Pastki челюстьning движениеsi суставga bog‘liq.

2
00:00:17,000 --> 00:00:20,500 [speed:fast]
Tegsiz blok.

3
00:00:21,000 --> 00:00:25,000 [speed:slow] [yangi:enamel=tish emali] [takror:зуб=tish]
Enamel зубни himoya qiladi.
"""


def test_parse_multiple_tags_stress_and_apostrophes():
    blocks = translation.parse_learning_srt(SAMPLE)
    assert [b["index"] for b in blocks] == [1, 2, 3]
    b1 = blocks[0]
    assert b1["start"] == 10.0 and b1["end"] == 16.0
    assert b1["text"] == "Pastki челюстьning движениеsi суставga bog‘liq."
    assert b1["words"] == [
        {"kind": "yangi", "lemma": "че́люсть", "meaning": "jag‘"},
        {"kind": "takror", "lemma": "суста́в", "meaning": "bo‘g‘im"},
    ]


def test_untagged_block_and_speed_tag_coexist():
    blocks = translation.parse_learning_srt(SAMPLE)
    assert blocks[1]["words"] == []
    assert blocks[2]["words"][0] == {"kind": "yangi", "lemma": "enamel", "meaning": "tish emali"}
    assert blocks[2]["words"][1] == {"kind": "takror", "lemma": "зуб", "meaning": "tish"}


def test_ascii_apostrophe_and_spaces_trimmed():
    srt = "1\n00:00:01,000 --> 00:00:02,000 [yangi:  тиш  = o'tkir tish ]\nтишlar\n"
    blocks = translation.parse_learning_srt(srt)
    assert blocks[0]["words"] == [{"kind": "yangi", "lemma": "тиш", "meaning": "o'tkir tish"}]


@pytest.mark.parametrize("line, fragment", [
    ("00:00:01,000 --> 00:00:02,000 [yangi:зуб=tish", "yopilmagan qavs"),
    ("00:00:01,000 --> 00:00:02,000 yangi:зуб=tish]", "yopilmagan qavs"),
    ("00:00:01,000 --> 00:00:02,000 [yangi:зуб]", "noto'g'ri formatdagi teg"),
    ("00:00:01,000 --> 00:00:02,000 [takror зуб=tish]", None),
    ("00:00:01,000 --> 00:00:02,000 [takror:зуб=tish:x]", "noto'g'ri formatdagi teg"),
    ("00:00:01,000 --> 00:00:02,000 [yangi: =tish]", "bo'sh"),
])
def test_broken_tags_are_errors_with_block_number(line, fragment):
    srt = f"1\n00:00:00,000 --> 00:00:01,000\nOK\n\n7\n{line}\nзуб matn\n"
    if fragment is None:
        # "[takror зуб=tish]" teg emas (ikki nuqta yo'q) - boshqa teg kabi e'tiborsiz qoladi
        assert translation.parse_learning_srt(srt)[1]["words"] == []
        return
    with pytest.raises(translation.LearningSrtError) as exc:
        translation.parse_learning_srt(srt)
    assert str(exc.value).startswith("7-blok")
    assert fragment in str(exc.value)


def test_other_tags_ignored():
    srt = "1\n00:00:01,000 --> 00:00:02,000 [speed:fast] [note:abc]\nmatn\n"
    assert translation.parse_learning_srt(srt)[0]["words"] == []


def test_found_in_text_rule():
    assert translation.lemma_found_in_text("че́люсть", "Pastki челюстьning harakati")
    assert translation.lemma_found_in_text("ёлка", "ЕЛКИ")
    assert translation.lemma_found_in_text("enamel", "Enamels are hard")
    assert not translation.lemma_found_in_text("сустав", "Pastki челюстьning harakati")


def test_warnings():
    srt = SAMPLE + """
4
00:00:26,000 --> 00:00:28,000 [yangi:сустав=qo‘shimcha] [takror:челюсть=jag‘]
Boshqa gap.
"""
    warnings = translation.learning_srt_warnings(translation.parse_learning_srt(srt))
    reasons = [(w["block"], w["reason"]) for w in warnings]
    assert (4, "«сустав» so'zi blok matnida topilmadi.") in reasons
    assert (4, "«челюсть» so'zi blok matnida topilmadi.") in reasons
    assert any("суста́в» turli joylarda turli ma'no" in r for _, r in reasons)
    assert any("че́люсть» 1-blokda yangi, 4-blokda takror" in r for _, r in reasons)
    assert not any(b == 1 for b, _ in reasons)


def test_more_than_20_new_words_warning():
    tags = " ".join(f"[yangi:слово{i}=so‘z{i}]" for i in range(21))
    text = " ".join(f"слово{i}" for i in range(21))
    srt = f"1\n00:00:01,000 --> 00:00:09,000 {tags}\n{text}\n"
    warnings = translation.learning_srt_warnings(translation.parse_learning_srt(srt))
    assert [w["reason"] for w in warnings] == ["Yangi so'zlar soni 21 ta - 20 tadan ko'p."]


def test_word_lists_first_appearance_order_and_dedup():
    srt = SAMPLE + "\n4\n00:00:26,000 --> 00:00:28,000 [yangi:челюсть=jag‘]\nчелюсть\n"
    lists = translation.learning_word_lists(translation.parse_learning_srt(srt))
    assert [w["lemma"] for w in lists["new"]] == ["че́люсть", "enamel"]
    assert [w["lemma"] for w in lists["repeat"]] == ["суста́в", "зуб"]
    assert [w["occurrences"] for w in lists["new"]] == [2, 1]
    assert [w["occurrences"] for w in lists["repeat"]] == [1, 1]


def test_occurrence_count_handles_stress_case_inflection_and_phrases():
    texts = ["ЧЕЛЮСТЬ va челюстьning harakati", "Елки, ёлка. katta oziq tishlar"]
    assert translation.lemma_occurrence_count("че́люсть", texts) == 2
    assert translation.lemma_occurrence_count("ёлка", texts) == 2
    assert translation.lemma_occurrence_count("katta oziq tish", texts) == 1


def test_parse_srt_direct_unchanged_on_tagged_srt():
    segs = translation.parse_srt_direct(SAMPLE)
    assert [s["text"] for s in segs] == [b["text"] for b in translation.parse_learning_srt(SAMPLE)]
    assert "speed_tag" not in segs[0]
    assert segs[1]["speed_tag"] == "fast" and segs[2]["speed_tag"] == "slow"
    assert "[" not in segs[0]["text"]


def test_replace_srt_block_texts_keeps_tags():
    out = translation.replace_srt_block_texts(SAMPLE, ["Yangi matn челюсть", "Ikki", "Uch зуб enamel"])
    blocks = translation.parse_learning_srt(out)
    assert [b["text"] for b in blocks] == ["Yangi matn челюсть", "Ikki", "Uch зуб enamel"]
    assert blocks[0]["words"] == translation.parse_learning_srt(SAMPLE)[0]["words"]
    assert translation.parse_srt_direct(out)[1]["speed_tag"] == "fast"


def test_words_vtt_uses_freeze_points_then_intro_offset():
    blocks = translation.parse_learning_srt(SAMPLE)
    freeze = [{"time": 12.0, "duration": 1.5}]
    cues = learning.words_cues(blocks, freeze, offset=20.0)
    assert len(cues) == 2  # tegsiz blok uchun cue yo'q
    assert cues[0]["start"] == 30.0 and cues[0]["end"] == 37.5
    assert cues[1]["start"] == 42.5 and cues[1]["end"] == 46.5
    vtt = learning.build_words_vtt(cues)
    assert vtt.startswith("WEBVTT")
    assert "00:00:30.000 --> 00:00:37.500 line:5% position:95% align:end" in vtt
    assert "<c.yangi>че́люсть — jag‘</c>" in vtt
    assert "<c.takror>суста́в — bo‘g‘im</c>" in vtt
    assert "</c>   •   <c.takror>" in vtt
    assert "</c>\n<c.takror>" not in vtt
    assert "::cue(.yangi) { color: #FFD400; }" in vtt


def test_learning_subtitles_and_words_share_time_source():
    blocks = translation.parse_learning_srt(SAMPLE)
    freeze = [{"time": 12.0, "duration": 1.5}]
    subs = learning.shifted_segments(translation.parse_srt_direct(SAMPLE), freeze, 4.0)
    cues = learning.words_cues(blocks, freeze, 4.0)
    assert (subs[0]["start"], subs[0]["end"]) == (cues[0]["start"], cues[0]["end"])
    assert (subs[2]["start"], subs[2]["end"]) == (cues[1]["start"], cues[1]["end"])


def test_words_ass():
    cues = learning.words_cues(translation.parse_learning_srt(SAMPLE))
    ass = learning.build_words_ass(cues, 1280, 720)
    assert "PlayResX: 1280" in ass and "PlayResY: 720" in ass
    assert re.search(r"Style: Yangi,DejaVu Sans,\d+,&H0000D4FF,.*,9,", ass)
    assert re.search(r"Style: Takror,DejaVu Sans,\d+,&H00FFFFFF,.*,9,", ass)
    assert "Dialogue: 0,0:00:10.00,0:00:16.00,Yangi,,0,0,0,,{\\rYangi}че́люсть — jag‘   •   {\\rTakror}" in ass
    assert "\\N{\\rTakror}" not in ass


@pytest.mark.parametrize("srt_name, expected", [
    ("Dars_01_LEARNING.srt", "Dars_01"),
    ("Dars_01_learning.SRT", "Dars_01"),
    ("Челюсть о‘qish.srt", "Челюсть о‘qish"),
    ("lesson.srt", "lesson"),
    ("_LEARNING.srt", "fallback"),
])
def test_asos_name(srt_name, expected):
    assert learning.asos_name(srt_name, "fallback") == expected


def test_content_disposition_rfc5987():
    header = learning.content_disposition("Челюсть o‘qish_learning.mp4")
    assert header.startswith('attachment; filename="')
    assert "filename*=UTF-8''%D0%A7%D0%B5%D0%BB%D1%8E%D1%81%D1%82%D1%8C%20o%E2%80%98qish_learning.mp4" in header


def test_intro_timing_formula():
    assert intro.repeat_screen_seconds(8) == 12.0
    assert intro.repeat_screen_seconds(5) == pytest.approx(9.0)
    assert intro.repeat_screen_seconds(3) == pytest.approx(6.6)
    assert intro.repeat_screen_seconds(2) == 6.0
    assert intro.card_seconds(0.7, 0.9) == pytest.approx(0.5 + 0.7 + 0.6 + 0.9 + 0.6 + 0.7 + 1.2)


def test_intro_plan():
    lists = translation.learning_word_lists(translation.parse_learning_srt(SAMPLE))
    plan = intro.plan_intro(lists)
    assert [p["kind"] for p in plan] == ["title", "repeat", "title", "card", "card"]
    assert plan[0]["text"] == "Takrorlash: 2 ta so‘z" and plan[2]["text"] == "Yangi so‘zlar: 2 ta"
    assert intro.plan_intro({"new": [], "repeat": []}) == []
    only_new = intro.plan_intro({"new": lists["new"], "repeat": []})
    assert [p["kind"] for p in only_new] == ["title", "card", "card"]
    many = intro.plan_intro({"new": [], "repeat": lists["repeat"] * 6})
    assert [p.get("seconds") for p in many if p["kind"] == "repeat"] == [12.0, pytest.approx(7.8)]


def test_original_language_and_stress_setting():
    assert intro.is_cyrillic("че́люсть") and not intro.is_cyrillic("enamel")
    assert intro.original_tts_text("че́люсть", False) == "че́люсть"
    assert intro.original_tts_text("че́люсть", True) == "челюсть"
