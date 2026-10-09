"""So'z vaqtlaridan original SRT bloklarini yasash va uydirma matnni olib tashlash.

OpenAI Whisper (timestamp_granularities=word) va ElevenLabs Scribe uchun umumiy.
So'z formati (videos.transcript_words): {"w": matn, "s": boshi, "e": oxiri, "spk": spiker yoki None}.
"""
import difflib
import re

from timing_contract import (
    DISPLAY_MAX_CHARS, DISPLAY_MAX_SEC, MIN_SENTENCE_BLOCK_SEC, PAUSE_SPLIT_SEC, ends_sentence,
)

# Jimlikda Whisper/Scribe "uydiradigan" iboralar (kichik harf, tinish belgisiz solishtiriladi).
# Sozlamalarda (hallucination_phrases) qo'shimcha iboralar berilishi mumkin.
DEFAULT_HALLUCINATION_PHRASES = [
    "подпишись на канал", "подписывайтесь на канал", "подписывайтесь на наш канал",
    "спасибо за просмотр", "продолжение следует", "субтитры сделал", "субтитры создавал",
    "редактор субтитров", "корректор", "ставьте лайки", "до новых встреч",
    "thanks for watching", "thank you for watching", "subscribe to my channel",
]
# Bitta blokdagi eng qisqa vaqt (0 soniyalik bloklar bo'lmasin).
MIN_BLOCK_SEC = 0.3

_NORM_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", _NORM_RE.sub(" ", (text or "").lower())).strip()


def attach_punctuation(words: list, segment_text: str) -> list:
    """Whisper so'zlarida tinish belgisi yo'q - segment matnidagi so'zlar
    (tinish belgilari bilan) vaqt bo'yicha so'zlarga moslab qo'yiladi."""
    tokens = (segment_text or "").split()
    if not tokens or not words:
        return words
    if len(tokens) == len(words):
        return [{**w, "w": t} for w, t in zip(words, tokens)]
    a = [normalize(w["w"]) for w in words]
    b = [normalize(t) for t in tokens]
    out = [dict(w) for w in words]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                out[i1 + k]["w"] = tokens[j1 + k]
    return out


def _block_from(words: list) -> dict:
    block = {"start": round(words[0]["s"], 3), "end": round(max(w["e"] for w in words), 3),
             "text": " ".join(w["w"] for w in words).strip()}
    if words[0].get("spk") is not None:
        block["speaker"] = words[0]["spk"]
    return block


def _best_split(words: list) -> int:
    """Uzun blokni qayerdan bo'lish: vergul/tinish belgisidan keyin yoki eng uzun
    pauzada (blokning ikkinchi yarmiga yaqinroq). Qaytaradi: yangi blok boshlanadigan indeks."""
    best, best_score = len(words) - 1, -1.0
    for i in range(1, len(words)):
        pause = words[i]["s"] - words[i - 1]["e"]
        punct = 0.3 if words[i - 1]["w"].rstrip().endswith((",", ";", ":", "-", "–", "—")) else 0.0
        position = i / len(words)
        score = pause + punct + (0.05 if position >= 0.4 else 0.0)
        if score > best_score:
            best, best_score = i, score
    return best


def build_segments_from_words(words: list) -> list:
    """So'zlardan original SRT bloklari:
    - pauza >= PAUSE_SPLIT_SEC - yangi blok;
    - gap tugash belgisi va blok >= MIN_SENTENCE_BLOCK_SEC - yangi blok;
    - spiker almashsa - yangi blok (bir blokda bitta spiker);
    - DISPLAY_MAX_SEC / DISPLAY_MAX_CHARS dan oshsa - eng yaqin pauza/vergulda bo'linadi.
    Blok vaqti = birinchi so'z boshi ... oxirgi so'z oxiri."""
    words = [w for w in words if (w.get("w") or "").strip()]
    words.sort(key=lambda w: w["s"])
    blocks, current = [], []

    def close():
        if current:
            blocks.append(_block_from(current))
            current.clear()

    for w in words:
        w = {**w, "w": w["w"].strip(), "e": max(w["e"], w["s"])}
        if current:
            prev = current[-1]
            if (w["s"] - prev["e"] >= PAUSE_SPLIT_SEC or w.get("spk") != current[0].get("spk")
                    or (ends_sentence(prev["w"]) and prev["e"] - current[0]["s"] >= MIN_SENTENCE_BLOCK_SEC)):
                close()
        current.append(w)
        text_len = sum(len(x["w"]) + 1 for x in current) - 1
        while len(current) > 1 and (current[-1]["e"] - current[0]["s"] > DISPLAY_MAX_SEC
                                    or text_len > DISPLAY_MAX_CHARS):
            cut = _best_split(current)
            head, tail = current[:cut], current[cut:]
            blocks.append(_block_from(head))
            current[:] = tail
            text_len = sum(len(x["w"]) + 1 for x in current) - 1
    close()
    # 0 soniyalik bloklar bo'lmasin (keyingi blokka tegmagan holda cho'ziladi).
    for i, b in enumerate(blocks):
        if b["end"] - b["start"] < MIN_BLOCK_SEC:
            limit = blocks[i + 1]["start"] if i + 1 < len(blocks) else b["start"] + MIN_BLOCK_SEC
            b["end"] = round(max(b["end"], min(b["start"] + MIN_BLOCK_SEC, limit)), 3)
    return blocks


def _matches_phrase(text: str, phrases: list) -> str:
    norm = normalize(text)
    if not norm:
        return ""
    for phrase in phrases:
        p = normalize(phrase)
        # Ibora blokning asosiy qismi bo'lsa (uzun gap ichida tasodifan uchrasa - tegilmaydi).
        if p and p in norm and len(p) >= 0.6 * len(norm):
            return phrase
    return ""


def remove_hallucinations(blocks: list, words: list, phrases: list = None, repetition_check=None):
    """Uydirma bloklarni natijadan chiqaradi. Qaytaradi: (qolgan bloklar, qolgan
    so'zlar, olib tashlanganlar). Olib tashlangan har biri flagged_issues ga
    "removed" holatida yoziladi - foydalanuvchi "Qaytarish" bilan qaytara oladi."""
    phrases = list(DEFAULT_HALLUCINATION_PHRASES) + [p for p in (phrases or []) if p.strip()]
    kept, removed = [], []
    for b in blocks:
        reason = ""
        phrase = _matches_phrase(b["text"], phrases)
        if phrase:
            reason = f"Uydirma ibora: “{phrase}”"
        elif repetition_check:
            suspicious, rep = repetition_check(b["text"])
            if suspicious:
                reason = f"Takrorlanish: “{rep}”"
        if reason:
            removed.append({"kind": "removed", "status": "removed", "start": b["start"], "end": b["end"],
                            "detail": reason, "segment": b})
        else:
            kept.append(b)
    removed_ranges = [(r["start"], r["end"]) for r in removed]
    kept_words = [w for w in words if not any(s - 0.001 <= w["s"] <= e + 0.001 for s, e in removed_ranges)]
    for r in removed:
        r["words"] = [w for w in words if r["start"] - 0.001 <= w["s"] <= r["end"] + 0.001]
    return kept, kept_words, removed


def whisper_chunk_words(data: dict, offset: float):
    """Whisper verbose_json javobidan so'zlar (tinish belgilari segment matnidan)
    va ishonchsiz (no_speech_prob > 0.6 va avg_logprob < -1.0) segmentlar.
    Qaytaradi: (so'zlar, olib tashlangan segmentlar)."""
    raw_words = [{"w": (w.get("word") or "").strip(), "s": float(w.get("start", 0)) + offset,
                  "e": float(w.get("end", 0)) + offset, "spk": None}
                 for w in data.get("words") or [] if (w.get("word") or "").strip()]
    if not raw_words:
        return [], []
    result, removed = [], []
    used = set()
    for seg in data.get("segments") or []:
        s, e = float(seg.get("start", 0)) + offset, float(seg.get("end", 0)) + offset
        idx = [i for i, w in enumerate(raw_words) if i not in used and s - 0.05 <= w["s"] < e + 0.05]
        used.update(idx)
        seg_words = attach_punctuation([raw_words[i] for i in idx], (seg.get("text") or "").strip())
        no_speech, logprob = seg.get("no_speech_prob"), seg.get("avg_logprob")
        if no_speech is not None and logprob is not None and no_speech > 0.6 and logprob < -1.0:
            if seg_words or (seg.get("text") or "").strip():
                text = (seg.get("text") or "").strip()
                removed.append({"kind": "removed", "status": "removed", "start": round(s, 3), "end": round(e, 3),
                                "detail": "Nutq yo'q joyda matn (no_speech_prob > 0.6, avg_logprob < -1.0)",
                                "segment": {"start": round(s, 3), "end": round(e, 3), "text": text},
                                "words": seg_words})
            continue
        result.extend(seg_words)
    result.extend(raw_words[i] for i in range(len(raw_words)) if i not in used)
    result.sort(key=lambda w: w["s"])
    return result, removed


def dedupe_overlap(chunks_words: list) -> list:
    """Bo'laklar ustma-ust kesilgan bo'lsa (jimlik topilmaganda 2 s overlap),
    keyingi bo'lakdagi takroriy so'zlar vaqt bo'yicha olib tashlanadi."""
    result = []
    for words in chunks_words:
        last_end = result[-1]["e"] if result else float("-inf")
        result.extend(w for w in words if w["s"] >= last_end - 0.05)
    return result
