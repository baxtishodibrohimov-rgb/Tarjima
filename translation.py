"""
O'zbekcha tarjima: avtomatik (OpenAI yoki Claude orqali, segment darajasida,
vaqt belgilarini o'zgartirmasdan) yoki foydalanuvchi tayyor matn/fayl yuklashi.
"""
import json
import re

import httpx

TRANSLATE_SYSTEM_PROMPT = (
    "You are a professional Russian/English-to-Uzbek translator specializing in "
    "dental and medical education content. You will receive a numbered list of "
    "speech-to-text (STT) segments, in chronological order. STT sometimes splits "
    "ONE sentence into several consecutive numbered segments purely because of "
    "pauses, breathing, or technical chunking - not because of meaning.\n\n"
    "Your job: group the segments into final subtitle blocks and translate each "
    "block into natural, fluent Uzbek (Latin script), preserving technical/dental "
    "terminology precisely.\n\n"
    "GROUPING RULE (read carefully):\n"
    "- Merge two or more CONSECUTIVE segments into ONE final block ONLY when they "
    "are mechanically split pieces of a SINGLE sentence (STT cut one sentence into "
    "pieces). This applies REGARDLESS of how many segments are involved (two, "
    "three, four, five, or more) and REGARDLESS of how long the merged block ends "
    "up being. Do NOT apply any fixed numeric limit (such as 'max 3 segments' or "
    "'max 90 characters') to decide whether to merge - the ONLY criterion is "
    "whether it is truly one sentence mechanically split by STT.\n"
    "- NEVER merge two segments that are independent, grammatically complete "
    "sentences, even if they are on the same topic or closely related.\n"
    "- A segment that is already a complete sentence on its own stays as its own "
    "block (source_indices with a single index).\n"
    "- The only exception in the other direction: if a single underlying sentence "
    "is extremely long, you may split ITS translation across two consecutive "
    "final blocks at a natural pause, so each subtitle stays readable - use this "
    "rarely, only when one block would otherwise be too long or slow to read.\n\n"
    "Every original segment number from 1 to N must belong to EXACTLY ONE final "
    "block - no segment may be skipped, and no segment may appear in more than "
    "one block. List final blocks in chronological order; the source_indices "
    "inside each block must be a contiguous run of consecutive original segment "
    "numbers.\n\n"
    "Respond ONLY with a JSON array of objects, one per final block, in "
    "chronological order, each shaped exactly as:\n"
    '{"source_indices": [<1-based original segment numbers covered by this '
    'block>], "text": "<Uzbek translation of the merged block>"}\n'
    "No commentary, no markdown, just the JSON array."
)


def _build_system_prompt(extra_instructions: str = "", extra_context: str = "") -> str:
    parts = [TRANSLATE_SYSTEM_PROMPT]
    if extra_instructions:
        parts.append("Additional instructions from the user (always follow these):\n" + extra_instructions)
    if extra_context:
        parts.append("Additional context / glossary notes learned from previous corrections:\n" + extra_context)
    return "\n\n".join(parts)


def estimate_translation_cost(input_chars: int, output_chars: int,
                               provider: str = "openai") -> float:
    # taxminan 4 belgi = 1 token
    input_tokens = input_chars / 4
    output_tokens = output_chars / 4
    if provider == "claude":
        # Claude Haiku 4.5: $1/1M kirish, $5/1M chiqish
        return round((input_tokens / 1_000_000) * 1.0 + (output_tokens / 1_000_000) * 5.0, 6)
    # OpenAI gpt-4o-mini: ~$0.15/1M kirish, ~$0.60/1M chiqish
    return round((input_tokens / 1_000_000) * 0.15 + (output_tokens / 1_000_000) * 0.60, 6)


def _max_tokens_for(segment_count: int) -> int:
    """Segmentlar soniga qarab javob uchun yetarli max_tokens hisoblaydi
    (taxminan 1 segment ~ 150-200 token chiqish, minimal 4096)."""
    return min(16384, max(4096, segment_count * 200))


async def translate_segments_via_openai(client: httpx.AsyncClient, api_key: str, segments: list,
                                         extra_instructions: str = "", extra_context: str = ""):
    """segments: [{"start":..,"end":..,"text":..}, ...] -> shu segmentlarni qamrab oluvchi
    yakuniy bloklar ro'yxati: [{"source_indices":[0-based...], "start":.., "end":.., "text":..}, ...]."""
    numbered = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(segments))
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": _build_system_prompt(extra_instructions, extra_context)},
            {"role": "user", "content": numbered},
        ],
        "temperature": 0.3,
        "max_tokens": _max_tokens_for(len(segments)),
    }
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body, timeout=180,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI xatosi ({resp.status_code}): {resp.text[:600]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    blocks = _parse_semantic_blocks(content, segments)
    usage = data.get("usage", {})
    usage_norm = {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0)}
    return blocks, usage_norm


async def translate_segments_via_claude(client: httpx.AsyncClient, api_key: str, segments: list,
                                         extra_instructions: str = "", extra_context: str = "",
                                         model: str = "claude-haiku-4-5-20251001"):
    """Claude (Anthropic) orqali xuddi shu vazifa - segmentlarni semantik bloklarga
    guruhlab tarjima qilish."""
    numbered = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(segments))
    body = {
        "model": model,
        "max_tokens": _max_tokens_for(len(segments)),
        "system": _build_system_prompt(extra_instructions, extra_context),
        "messages": [{"role": "user", "content": numbered}],
        "temperature": 0.3,
    }
    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json=body, timeout=180,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Claude xatosi ({resp.status_code}): {resp.text[:600]}")
    data = resp.json()
    content_blocks = data.get("content", [])
    content = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text").strip()
    content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
    blocks = _parse_semantic_blocks(content, segments)
    usage = data.get("usage", {})
    usage_norm = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}
    return blocks, usage_norm


def _parse_semantic_blocks(content: str, segments: list) -> list:
    """LLM javobini ({"source_indices":[1-based...], "text":..} obyektlari massivi)
    o'qiydi va tekshiradi, so'ng har bir blok uchun start/end'ni SEGMENTS asosida
    o'zimiz hisoblab, yakuniy blok ro'yxatini qaytaradi:
    [{"source_indices": [0-based...], "start":.., "end":.., "text":..}, ...]

    Tekshiruvlar:
      a) har bir element {"source_indices":[...], "text":...} shaklida bo'lishi;
      b) source_indices barcha bloklar bo'yicha 0..len(segments)-1 ni to'liq va
         takrorsiz qoplashi (yetishmayotgan/takrorlangan indekslar aniq ko'rsatiladi);
      c) start/end - segmentlardan hisoblanadi, shu sababli har doim qamrov
         ichida bo'ladi (LLM'ga ishonmaymiz);
      d) bloklar xronologik tartibda va bir-birini qoplamasligi (source_indices
         har bir blok ichida ketma-ket bo'lishi kerak, bloklar orasida orqaga
         qaytish taqiqlanadi);
      e) har bir qoidabuzarlik uchun aniq RuntimeError xabari.
    """
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", content, re.S)
        if not match:
            raise RuntimeError("Tarjima javobini o'qib bo'lmadi (JSON topilmadi).")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("Tarjima javobi bo'sh yoki JSON massiv emas.")

    n = len(segments)
    seen = set()
    blocks = []
    prev_max = -1
    for i, item in enumerate(parsed):
        if not isinstance(item, dict) or "source_indices" not in item or "text" not in item:
            raise RuntimeError(
                f"{i + 1}-blok formati noto'g'ri: 'source_indices' va 'text' maydonlari topilmadi."
            )
        idxs = item["source_indices"]
        if (not isinstance(idxs, list) or not idxs
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in idxs)):
            raise RuntimeError(f"{i + 1}-blok: source_indices butun sonlar ro'yxati bo'lishi kerak ({idxs!r}).")
        idxs0 = sorted(x - 1 for x in idxs)
        if idxs0[0] < 0 or idxs0[-1] >= n:
            raise RuntimeError(
                f"{i + 1}-blok: source_indices chegaradan tashqari {idxs} (segmentlar soni: {n})."
            )
        if idxs0[-1] - idxs0[0] + 1 != len(idxs0):
            raise RuntimeError(
                f"{i + 1}-blok: source_indices ketma-ket bo'lmagan segmentlarni o'z ichiga oladi {idxs}."
            )
        if idxs0[0] <= prev_max:
            raise RuntimeError(
                f"{i + 1}-blok: bloklar tartibsiz yoki bir-birini qoplaydi "
                f"(oldingi blok {prev_max + 1}-segmentgacha, bu blok {idxs0[0] + 1}-segmentdan boshlanadi)."
            )
        dup = [x for x in idxs0 if x in seen]
        if dup:
            raise RuntimeError(
                f"{', '.join(str(x + 1) for x in dup)}-segment(lar) bir nechta blokka kiritilgan (takroriy)."
            )
        seen.update(idxs0)
        prev_max = idxs0[-1]
        text = item["text"]
        if not isinstance(text, str):
            raise RuntimeError(f"{i + 1}-blok: 'text' maydoni matn (string) bo'lishi kerak.")
        start = min(segments[x]["start"] for x in idxs0)
        end = max(segments[x]["end"] for x in idxs0)
        blocks.append({"source_indices": idxs0, "start": start, "end": end, "text": text})

    missing = [x for x in range(n) if x not in seen]
    if missing:
        shown = ", ".join(str(x + 1) for x in missing[:20])
        more = " ..." if len(missing) > 20 else ""
        raise RuntimeError(
            f"Tarjima natijasida {len(missing)} ta original segment qamrab olinmagan: {shown}{more}."
        )
    return blocks


async def translate_segments_via_api(client: httpx.AsyncClient, api_key: str, segments: list):
    """Eskilik uchun (backward-compat) - OpenAI orqali tarjima."""
    return await translate_segments_via_openai(client, api_key, segments)


def parse_manual_translation(content: str, expected_segments: list) -> list:
    """Foydalanuvchi yuklagan/joylashtirgan tarjima matnini segmentlarga moslaydi.
    SRT formatida bo'lsa vaqt belgilaridan foydalaniladi (lekin asl segment
    vaqtlari saqlab qolinadi); oddiy matn bo'lsa bo'sh qatorlar bilan ajratilgan
    bloklar asl segmentlar tartibi bilan mos deb hisoblanadi."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    time_re = re.compile(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")

    if time_re.search(normalized):
        blocks = re.split(r"\n\s*\n", normalized)
        texts = []
        for block in blocks:
            lines = [l for l in block.split("\n") if l.strip() != ""]
            if not lines:
                continue
            idx = 1 if re.match(r"^\d+$", lines[0].strip()) else 0
            time_idx = idx if idx < len(lines) and time_re.search(lines[idx]) else None
            if time_idx is None:
                continue
            text = " ".join(lines[time_idx + 1:]).strip()
            if text:
                texts.append(text)
    else:
        blocks = re.split(r"\n\s*\n", normalized)
        texts = [b.strip().replace("\n", " ") for b in blocks if b.strip()]

    if len(texts) != len(expected_segments):
        raise ValueError(
            f"Yuklangan tarjimada {len(texts)} ta bo'lak topildi, lekin original matnda "
            f"{len(expected_segments)} ta segment bor. Iltimos, segmentlar sonini moslashtiring "
            f"(har bir original segment uchun bitta bo'lak, bo'sh qator bilan ajratilgan, yoki SRT formatida)."
        )
    return texts


def parse_srt_direct(content: str) -> list:
    """Tayyor SRT faylini o'z vaqt belgilari bilan to'g'ridan-to'g'ri o'qiydi
    (original transkripsiya bo'laklar soniga bog'liq emas). Foydalanuvchi
    o'zi tayyorlagan o'zbekcha SRT faylini shu ko'rinishda yuklashi mumkin."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    time_re = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
    blocks = re.split(r"\n\s*\n", normalized)
    segments = []
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip() != ""]
        if not lines:
            continue
        idx = 1 if re.match(r"^\d+$", lines[0].strip()) else 0
        if idx >= len(lines):
            continue
        m = time_re.search(lines[idx])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = " ".join(lines[idx + 1:]).strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})
    if not segments:
        raise ValueError("SRT faylida to'g'ri formatdagi bloklar topilmadi.")
    return segments


def fmt_hms(total_sec: float) -> str:
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = int(total_sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def match_segments_by_timestamp(new_srt_content: str, target_indices: list, original_segments: list,
                                 tolerance: float = 0.75):
    """'Xatoni to'g'irlash': foydalanuvchi ko'rsatgan segment raqamlari uchun,
    original vaqt belgisiga mos keladigan bo'lakni yangi yuklangan SRT'dan
    qidiradi. Vaqt mos kelmasa - aniq xato qaytaradi (indeks + kutilgan vaqt).
    Qaytaradi: (matched: {index: text}, errors: [{"index":.., "expected_time":..}])
    """
    new_segments = parse_srt_direct(new_srt_content)
    matched = {}
    errors = []
    for idx in target_indices:
        if idx < 0 or idx >= len(original_segments):
            errors.append({"index": idx, "reason": "Bunday segment raqami mavjud emas."})
            continue
        orig = original_segments[idx]
        found = None
        for cand in new_segments:
            if abs(cand["start"] - orig["start"]) <= tolerance and abs(cand["end"] - orig["end"]) <= tolerance:
                found = cand
                break
        if found:
            matched[idx] = found["text"]
        else:
            errors.append({
                "index": idx,
                "reason": (f"{idx + 1}-segment vaqt belgisi "
                           f"({fmt_hms(orig['start'])}\u2192{fmt_hms(orig['end'])}) "
                           f"yuklangan faylda topilmadi."),
                "expected_start": orig["start"], "expected_end": orig["end"],
            })
    return matched, errors
