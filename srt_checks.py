"""Tarjima va Learning SRT yuklashdagi tekshiruvlar (MASTER INSTRUKSIYA bilan moslik).

Hammasi faqat OGOHLANTIRISH - yuklash rad etilmaydi. Format (Learning bilan bir xil):
[{"block": raqam yoki None, "time": soniya yoki None, "reason": matn}].
"""
import bisect

import transcription
from timing_contract import SLOWMO_MIN_RATE, TTS_CPS_ESTIMATE, ends_sentence

MAX_RUN_WITHOUT_END = 4
MAX_SENTENCE_SEC = 60.0
EARLY_START_SEC = 0.5
MAX_LISTED = 30


def _warn(block, time, reason):
    return {"block": block, "time": round(time, 2) if time is not None else None, "reason": reason}


def _sentences(blocks: list) -> list:
    """Gaplar (faqat tinish belgisi bo'yicha, spikerga qaramay) - [(birinchi, oxirgi blok indeksi)]."""
    result, start = [], None
    for i, b in enumerate(blocks):
        if not (b.get("text") or "").strip():
            if start is not None:
                result.append((start, i - 1))
                start = None
            continue
        if start is None:
            start = i
        if ends_sentence(b["text"]):
            result.append((start, i))
            start = None
    if start is not None:
        result.append((start, len(blocks) - 1))
    return result


def translation_warnings(blocks: list, originals: list, words: list = None, video_duration: float = 0.0) -> list:
    warnings = []
    # 1) Gap chegarasi belgilari.
    run = 0
    for i, b in enumerate(blocks):
        text = (b.get("text") or "").strip()
        run = 0 if (not text or ends_sentence(text)) else run + 1
        if run == MAX_RUN_WITHOUT_END:
            warnings.append(_warn(i + 1 - (MAX_RUN_WITHOUT_END - 1), b["start"],
                                  f"Ketma-ket {MAX_RUN_WITHOUT_END}+ blok gap tugash belgisisiz (. ? ! …) - "
                                  f"ovoz bitta uzun gap bo'lib o'qiladi."))
    sentences = _sentences(blocks)
    for first, last in sentences:
        span = blocks[last]["end"] - blocks[first]["start"]
        if span > MAX_SENTENCE_SEC:
            warnings.append(_warn(first + 1, blocks[first]["start"], f"Gap {span:.0f} s davom etadi (60 s dan uzun)."))
        speakers = {blocks[k].get("speaker") for k in range(first, last + 1) if blocks[k].get("speaker") is not None}
        if len(speakers) > 1:
            warnings.append(_warn(first + 1, blocks[first]["start"], "Bitta gap ichida spiker almashgan."))

    # 2) Gap boshi nutqdan oldin (MASTER 9).
    speech = sorted((float(s["start"]), float(s["end"])) for s in originals or [] if s.get("end") is not None)
    if words:
        speech = sorted((float(w["s"]), float(w["e"])) for w in words)
    if speech:
        starts = [s for s, _ in speech]
        for first, _ in sentences:
            t = float(blocks[first]["start"])
            k = bisect.bisect_right(starts, t)
            inside = k > 0 and speech[k - 1][1] >= t - 0.05
            if not inside and k < len(starts) and starts[k] - t > EARLY_START_SEC:
                warnings.append(_warn(first + 1, t, f"Gap boshi original nutqdan {starts[k] - t:.1f} s oldin "
                                                    f"(ovoz spikerdan oldin eshitiladi)."))

    # 3) Spiker teglari.
    if transcription.speaker_count(originals) >= 2 and transcription.speaker_count(blocks) == 0:
        warnings.append(_warn(None, None, "Original matnda [spk:N] teglari bor, tarjimada yo'q - "
                                          "hamma gap bitta ovoz bilan o'qiladi."))

    # 4) Taxminiy video sekinlashishi (faqat ma'lumot).
    voiced = sentences
    slow = []
    for idx, (first, last) in enumerate(voiced):
        start = float(blocks[first]["start"])
        nxt = float(blocks[voiced[idx + 1][0]]["start"]) if idx + 1 < len(voiced) else (video_duration or 0)
        if nxt <= start:
            continue
        text = " ".join((blocks[k].get("text") or "") for k in range(first, last + 1))
        need = len(text.strip()) / TTS_CPS_ESTIMATE
        if need > 0 and (nxt - start) / need < SLOWMO_MIN_RATE:
            slow.append(_warn(first + 1, start, f"Ovoz uchun vaqt kam: video {(nxt - start) / need:.2f} tezlikkacha "
                                                f"sekinlashishi kerak bo'lardi (0.75 dan past - kutish bo'ladi)."))
    warnings.extend(slow)
    warnings.sort(key=lambda w: (w["block"] is not None, w["block"] or 0))
    if len(warnings) > MAX_LISTED:
        rest = len(warnings) - MAX_LISTED
        warnings = warnings[:MAX_LISTED] + [_warn(None, None, f"... yana {rest} ta ogohlantirish.")]
    return warnings


def learning_vs_uzbek_warnings(learning_blocks: list, uzbek_blocks: list) -> list:
    """Learning SRT UZBEK_FULL (asosiy o'zbekcha tarjima) bilan bir xil tuzilishda bo'lishi kerak."""
    if not uzbek_blocks:
        return []
    warnings = []
    if len(learning_blocks) != len(uzbek_blocks):
        warnings.append(_warn(None, None, f"Bloklar soni o'zbekcha tarjimadan farq qiladi "
                                          f"({len(learning_blocks)} va {len(uzbek_blocks)})."))
    shown = 0
    for i, (lb, ub) in enumerate(zip(learning_blocks, uzbek_blocks)):
        reasons = []
        if abs(lb["start"] - ub["start"]) > 0.05 or abs(lb["end"] - ub["end"]) > 0.05:
            reasons.append("vaqti")
        if ends_sentence(lb.get("text") or "") != ends_sentence(ub.get("text") or ""):
            reasons.append("gap tugash belgisi")
        if lb.get("speaker") != ub.get("speaker"):
            reasons.append("[spk] tegi")
        if reasons:
            shown += 1
            if shown <= 10:
                warnings.append(_warn(i + 1, lb["start"], f"O'zbekcha tarjimadagi blokdan farq: {', '.join(reasons)}."))
    if shown > 10:
        warnings.append(_warn(None, None, f"... yana {shown - 10} ta blok farq qiladi."))
    return warnings

