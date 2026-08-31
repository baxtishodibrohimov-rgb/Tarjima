"""
O'zbekcha tarjima: avtomatik (OpenAI yoki Claude orqali, segment darajasida,
vaqt belgilarini o'zgartirmasdan) yoki foydalanuvchi tayyor matn/fayl yuklashi.
"""
import json
import re

import httpx

TRANSLATE_SYSTEM_PROMPT = (
    "You are a professional Russian/English-to-Uzbek translator specializing in "
    "dental and medical education content. Translate each numbered segment into "
    "natural, fluent Uzbek (Latin script). Preserve technical/dental terminology "
    "precisely. Keep the same number of segments and the same order. "
    "Respond ONLY with a JSON array of strings, one translated string per input "
    "segment, in the same order. No commentary, no markdown, just the JSON array."
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


async def translate_segments_via_openai(client: httpx.AsyncClient, api_key: str, segments: list,
                                         extra_instructions: str = "", extra_context: str = ""):
    """segments: [{"start":..,"end":..,"text":..}, ...] -> shu tartibda tarjima qilingan matnlar ro'yxati."""
    numbered = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(segments))
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": _build_system_prompt(extra_instructions, extra_context)},
            {"role": "user", "content": numbered},
        ],
        "temperature": 0.3,
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
    translated = _parse_translated_json(content, segments)
    usage = data.get("usage", {})
    usage_norm = {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0)}
    return translated, usage_norm


async def translate_segments_via_claude(client: httpx.AsyncClient, api_key: str, segments: list,
                                         extra_instructions: str = "", extra_context: str = "",
                                         model: str = "claude-haiku-4-5-20251001"):
    """Claude (Anthropic) orqali xuddi shu vazifa - segment-segment tarjima."""
    numbered = "\n".join(f"{i+1}. {s['text']}" for i, s in enumerate(segments))
    body = {
        "model": model,
        "max_tokens": 8192,
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
    translated = _parse_translated_json(content, segments)
    usage = data.get("usage", {})
    usage_norm = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}
    return translated, usage_norm


def _parse_translated_json(content: str, segments: list) -> list:
    try:
        translated = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", content, re.S)
        if not match:
            raise RuntimeError("Tarjima javobini o'qib bo'lmadi (JSON topilmadi).")
        translated = json.loads(match.group(0))
    if not isinstance(translated, list) or len(translated) != len(segments):
        raise RuntimeError(
            f"Tarjima natijasi segmentlar soniga mos kelmadi ({len(translated) if isinstance(translated, list) else '?'} "
            f"!= {len(segments)}). Qayta urinib ko'ring."
        )
    return translated


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
