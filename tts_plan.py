"""Ovozni gap (sentence) birligida joylashtirish rejasi - sof funksiyalar.

MASTER INSTRUKSIYA v8 qoidalari (timing_contract.py):
  - TTS birligi - gap: ketma-ket bloklar, gap tugash belgisi bilan yopiladi;
  - har gap ovozi videoda shu gapning original boshlanish joyida boshlanadi;
  - ovoz tezligi (tempo) lektorning o'z sur'atiga ergashadi va silliq o'zgaradi;
  - ovoz sig'masa - video sekinlashadi (slow), juda kam holatda kutadi (freeze).

Bu yerda fayl/baza/ffmpeg yo'q - tts.py shu rejani bajaradi.
"""
import bisect
import math
import re
import statistics

import transcription
from timing_contract import (
    BASE_TEMPO_MAX, BASE_TEMPO_MIN, PACE_WINDOW_SEC, PAUSE_SPLIT_SEC, SLOWMO_MIN_RATE, TEMPO_MAX,
    TEMPO_MIN, TEMPO_STEP, TTS_MAX_CHARS, ends_sentence,
)

# Uzun gap shu belgilardan keyin bo'linadi (eng yaxshisidan boshlab).
_SPLIT_PATTERNS = (re.compile(r"[;:]\s"), re.compile(r",\s"), re.compile(r"\s[-–—]\s"), re.compile(r"\s"))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def split_long_text(text: str, max_chars: int) -> list:
    """Provayder chegarasidan uzun gapni vergul/tire (bo'lmasa bo'sh joy)
    joyida bo'laklarga ajratadi. Matn o'zgarmaydi - faqat bo'linadi."""
    text = (text or "").strip()
    parts = []
    while len(text) > max_chars:
        cut = 0
        for pattern in _SPLIT_PATTERNS:
            positions = [m.end() for m in pattern.finditer(text, 0, max_chars + 1) if m.end() >= max_chars // 3]
            if positions:
                cut = positions[-1]
                break
        if not cut:
            cut = max_chars
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def build_units(blocks: list, provider: str) -> list:
    """Yakuniy SRT bloklaridan TTS birliklari (gap yoki uzun gap bo'lagi).

    Gap: ketma-ket bloklar, ends_sentence() bilan tugagan blokda yopiladi;
    spiker almashsa ham yangi gap boshlanadi (bitta gap = bitta spiker).
    Matni bo'sh blok - alohida "skipped" birlik (ovoz yo'q)."""
    max_chars = TTS_MAX_CHARS.get(provider, min(TTS_MAX_CHARS.values()))
    units, current = [], []
    state = {"sentence": 0}

    def flush():
        if not current:
            return
        text = " ".join(b["text"] for _, b in current)
        for part_index, part in enumerate(split_long_text(text, max_chars)):
            units.append({
                "sentence_index": state["sentence"], "part_index": part_index,
                "block_start": current[0][0], "block_end": current[-1][0],
                "start": float(current[0][1].get("start") or 0.0), "end": float(current[-1][1].get("end") or 0.0),
                "text": part, "speaker": current[0][1].get("speaker"), "skipped": False,
            })
        state["sentence"] += 1
        current.clear()

    for i, block in enumerate(blocks):
        text = re.sub(r"\s+", " ", (block.get("text") or "")).strip()
        if not text:
            flush()
            units.append({
                "sentence_index": state["sentence"], "part_index": 0, "block_start": i, "block_end": i,
                "start": float(block.get("start") or 0.0), "end": float(block.get("end") or 0.0),
                "text": "", "speaker": block.get("speaker"), "skipped": True,
            })
            state["sentence"] += 1
            continue
        if current and block.get("speaker") != current[0][1].get("speaker"):
            flush()
        current.append((i, {**block, "text": text}))
        if ends_sentence(text):
            flush()
    flush()
    return units


def sentence_ids_for_blocks(blocks: list) -> list:
    """Har blok qaysi gapga tegishli (UI'da gap chegarasini chizish uchun)."""
    ids = [0] * len(blocks)
    for u in build_units(blocks, "openai"):
        for i in range(u["block_start"], u["block_end"] + 1):
            ids[i] = u["sentence_index"]
    return ids


def group_sentences(units: list) -> list:
    """Bazadagi birliklarni (seg_index tartibida) gaplarga yig'adi.

    Yangi ishlar: sentence_index bo'yicha. Eski ishlar (har blok alohida
    yaratilgan, sentence_index yo'q): bloklar shu yerda ends_sentence() bilan
    gaplarga yig'iladi - eski audio fayllari qayta ishlatiladi (TTS so'rovi yo'q)."""
    units = sorted(units, key=lambda u: u["seg_index"])
    sentences = []
    if any(u.get("sentence_index") is not None for u in units):
        by_id = {}
        for u in units:
            sid = u["sentence_index"] if u.get("sentence_index") is not None else -1 - u["seg_index"]
            if sid not in by_id:
                by_id[sid] = {"units": [], "start": u["start_sec"] or 0.0, "speaker": u.get("speaker")}
                sentences.append(by_id[sid])
            by_id[sid]["units"].append(u)
        for s in sentences:
            s["units"].sort(key=lambda u: (u.get("part_index") or 0, u["seg_index"]))
    else:
        current = None
        for u in units:
            text = (u.get("text") or "").strip()
            if u["status"] == "skipped" or not text:
                current = None
                sentences.append({"units": [u], "start": u["start_sec"] or 0.0, "speaker": u.get("speaker")})
                continue
            if current is None or current["speaker"] != u.get("speaker"):
                current = {"units": [], "start": u["start_sec"] or 0.0, "speaker": u.get("speaker")}
                sentences.append(current)
            current["units"].append(u)
            if ends_sentence(text):
                current = None
    for s in sentences:
        s["voiced"] = any(u["status"] == "completed" and u.get("audio_path") for u in s["units"])
    sentences.sort(key=lambda s: s["start"])
    return sentences


def available_times(starts: list, video_duration: float) -> list:
    """A_i = keyingi gap boshi - gap boshi (oxirgisi uchun video oxiri;
    video uzunligi noma'lum bo'lsa cheksiz)."""
    result = []
    for i, start in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else (video_duration if video_duration > 0 else math.inf)
        result.append(max(nxt - start, 0.05))
    return result


# ---------------------------------------------------------------------------
#                    LEKTOR SUR'ATI VA TEMPO (2.4)
# ---------------------------------------------------------------------------

def pace_items_from_words(words: list) -> list:
    """transcript_words ([{"w","s","e","spk"}]) -> (start, end, belgilar, spiker)."""
    items = []
    for w in words or []:
        text = (w.get("w") or "").strip()
        if text and w.get("e") is not None and w.get("s") is not None and w["e"] >= w["s"]:
            items.append((float(w["s"]), float(w["e"]), len(text), w.get("spk")))
    items.sort()
    return items


def pace_items_from_segments(segments: list) -> list:
    """Original SRT bloklari (so'z vaqtlari yo'q bo'lsa)."""
    items = []
    for s in segments or []:
        text = (s.get("text") or "").strip()
        if text and s.get("end") is not None and s.get("start") is not None and s["end"] > s["start"]:
            items.append((float(s["start"]), float(s["end"]), len(text), s.get("speaker")))
    items.sort()
    return items


def window_pace(items: list, starts: list, center: float, speaker=None):
    """center +-PACE_WINDOW_SEC/2 oynadagi sur'at: belgilar / nutq vaqti
    (so'zlar davomiyligi + ular orasidagi PAUSE_SPLIT_SEC dan qisqa pauzalar)."""
    half = PACE_WINDOW_SEC / 2
    t0, t1 = center - half, center + half
    i = max(bisect.bisect_left(starts, t0) - 1, 0)
    chars = speech = 0.0
    prev_end = None
    while i < len(items) and items[i][0] < t1:
        s, e, n, spk = items[i]
        i += 1
        if e <= t0 or (speaker is not None and spk is not None and spk != speaker):
            continue
        cs, ce = max(s, t0), min(e, t1)
        if ce <= cs:
            continue
        frac = (ce - cs) / (e - s) if e > s else 1.0
        chars += n * frac
        speech += ce - cs
        if prev_end is not None and 0 < cs - prev_end < PAUSE_SPLIT_SEC:
            speech += cs - prev_end
        prev_end = ce
    return chars / speech if speech > 0.5 else None


def sentence_paces(sentences: list, items: list) -> list:
    starts = [it[0] for it in items]
    paces = []
    for s in sentences:
        center = (s["start"] + s.get("end", s["start"])) / 2
        paces.append(window_pace(items, starts, center, s.get("speaker")) if items else None)
    return paces


def compute_tempos(durations: list, available: list, paces: list, speakers: list):
    """tempo_i: base * pace_i / pace_ref (har spiker uchun alohida pace_ref),
    [TEMPO_MIN, TEMPO_MAX] ichida va ketma-ket gaplar orasida <= TEMPO_STEP.
    Qaytaradi: (tempos, base)."""
    ratios = [d / a for d, a in zip(durations, available) if d > 0 and a and not math.isinf(a)]
    base = _clamp(statistics.median(ratios), BASE_TEMPO_MIN, BASE_TEMPO_MAX) if ratios else BASE_TEMPO_MIN
    pace_ref = {}
    for spk in set(speakers):
        vals = [p for p, s in zip(paces, speakers) if s == spk and p]
        pace_ref[spk] = statistics.median(vals) if vals else None
    raw = []
    for p, spk in zip(paces, speakers):
        ref = pace_ref.get(spk)
        raw.append(_clamp(base * p / ref if p and ref else base, TEMPO_MIN, TEMPO_MAX))
    tempos = list(raw)
    # Silliqlash har spikerning o'z gaplari ketma-ketligida: oldinga, keyin orqaga
    # o'tish (orqaga o'tish |t_i - t_{i+1}| <= TEMPO_STEP ni kafolatlaydi).
    for spk in set(speakers):
        idx = [i for i, s in enumerate(speakers) if s == spk]
        for a, b in zip(idx, idx[1:]):
            tempos[b] = _clamp(tempos[b], tempos[a] - TEMPO_STEP, tempos[a] + TEMPO_STEP)
        for a, b in zip(reversed(idx[1:]), reversed(idx[:-1])):
            tempos[b] = _clamp(tempos[b], tempos[a] - TEMPO_STEP, tempos[a] + TEMPO_STEP)
    return [round(t, 4) for t in tempos], round(base, 4)


# ---------------------------------------------------------------------------
#                    VIDEO SEKINLASHTIRISH REJASI (2.3)
# ---------------------------------------------------------------------------

def plan_points(starts: list, available: list, needs: list) -> list:
    """Har gap: need_i <= A_i bo'lsa - joylanadi; aks holda [start, keyingi
    start] oralig'i sekinlashadi (extra = need - A, tezlik >= SLOWMO_MIN_RATE),
    qolgani keyingi gap boshida (shu gap ovozi tugaydigan manba vaqti) freeze."""
    points = []
    for start, a, need in zip(starts, available, needs):
        if math.isinf(a) or need <= a + 0.001:
            continue
        end = start + a
        extra = need - a
        slow_extra = min(extra, a / SLOWMO_MIN_RATE - a)
        if slow_extra > transcription.SLOW_MIN_EXTRA_SEC:
            points.append({"type": "slow", "start": round(start, 3), "end": round(end, 3),
                           "extra": round(slow_extra, 3)})
        else:
            slow_extra = 0.0
        rest = extra - slow_extra
        if rest > 0.001:
            # Juda qisqa freeze render qilinmaydi - ovozlar ustma-ust tushmasligi uchun
            # eng kam freeze uzunligigacha yaxlitlanadi.
            points.append({"type": "freeze", "time": round(end, 3),
                           "duration": round(max(rest, transcription.FREEZE_MIN_SEC + 0.01), 3)})
    return points
