"""
Video -> Matn: ffmpeg audio ajratish/bo'laklash, glossary bilan tuzatish,
Whisper API chaqiruvi va takrorlanish (hallucination) aniqlash.

Bu modul mavjud app.py dagi ishlaydigan mantiqni saqlab qoladi, faqat
persistent chunk-darajasidagi ishlash uchun moslashtirilgan.
"""
import difflib
import math
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import imageio_ffmpeg

from glossary_data import GLOSSARY
from storage import REPETITION_THRESHOLD

GLOSSARY_MATCH_THRESHOLD = 0.86

# ffmpeg jarayonlari uchun cheklovlar - agar shu vaqt ichida tugamasa, jarayon
# to'xtatiladi va aniq xato qaytariladi (aks holda 2 GB'gacha videolarda ffmpeg
# biror sababdan "osilib qolsa", bosqich abadiy "ishlanmoqda" holatida qotib qolar
# edi, hech qanday xato yoki signalsiz).
FFMPEG_PROBE_TIMEOUT = 120  # faqat metadata o'qish (Duration/fps) - deyarli tezkor bo'lishi kerak
FFMPEG_TIMEOUT = 1800  # haqiqiy ishlov berish (mux/encode) - 30 daqiqa


def ffmpeg_exe():
    """Avval tizimda o'rnatilgan ffmpeg'ni qidiradi (masalan `apt install ffmpeg` orqali -
    ARM/aarch64 serverlarda ham ishlaydi), topilmasa imageio-ffmpeg orqali o'ralgan
    tayyor (faqat x86_64 uchun) nusxaga tushadi."""
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def get_duration_seconds(path: Path) -> float:
    try:
        proc = subprocess.run(
            [ffmpeg_exe(), "-i", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
            timeout=FFMPEG_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stdout or "")
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def generate_thumbnail(input_path: Path, out_path: Path) -> bool:
    """Video o'rtasidan bitta kadr olib, kichik JPEG thumbnail yaratadi."""
    duration = get_duration_seconds(input_path)
    mid = max(duration / 2, 0.5)
    cmd = [
        ffmpeg_exe(), "-y", "-ss", str(mid), "-i", str(input_path),
        "-frames:v", "1", "-vf", "scale=320:-1", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and out_path.exists()


# Telegram Bot API'ning haqiqiy qat'iy limiti 2 GB - 1.9 GB'da bo'lish orasida
# xavfsizlik zaxirasi qoldiradi (segment muxer taxminiy hajm beradi, aniq emas).
TELEGRAM_MAX_PART_BYTES = int(1.9 * 1024 ** 3)


def split_video_by_size(input_path: Path, out_dir: Path, max_bytes: int = TELEGRAM_MAX_PART_BYTES) -> list:
    """Video faylni max_bytes'dan oshmaydigan bir necha qismga bo'ladi (qayta
    kodlamasdan, -c copy - tez ishlaydi). Fayl hajmi allaqachon max_bytes'dan
    kichik bo'lsa, o'zgarishsiz [input_path] qaytaradi.

    Diqqat: ishlatilayotgan ffmpeg build'ida segment muxer'ning bayt-hajm
    asosidagi bo'lish parametri (-segment_size) yo'q, shuning uchun o'rtacha
    bitreytdan vaqt oralig'i hisoblanadi - bitreyt notekis bo'lishi mumkinligi
    uchun 10% xavfsizlik zaxirasi bilan (max_bytes o'zi allaqachon 2 GB haqiqiy
    limitidan 1.9 GB'ga tushirilgan)."""
    input_path = Path(input_path)
    size = input_path.stat().st_size
    if size <= max_bytes:
        return [input_path]

    duration = get_duration_seconds(input_path)
    if duration <= 0:
        raise RuntimeError("Video davomiyligini aniqlab bo'lmadi, bo'laklarga bo'lib bo'lmaydi.")

    target_part_bytes = max_bytes * 0.9
    num_parts = max(2, math.ceil(size / target_part_bytes))
    part_duration = duration / num_parts

    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "part_%03d.mp4")
    cmd = [
        ffmpeg_exe(), "-y", "-i", str(input_path),
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(part_duration),
        "-reset_timestamps", "1", pattern,
    ]
    _run_ffmpeg(cmd, "videoni bo'laklarga bo'lish")
    parts = sorted(out_dir.glob("part_*.mp4"))
    if not parts:
        raise RuntimeError("Video bo'laklarga bo'linmadi (natija fayllar topilmadi).")

    oversized = [p for p in parts if p.stat().st_size > max_bytes]
    if oversized:
        raise RuntimeError(
            f"{len(oversized)} ta bo'lak {max_bytes // (1024**2)} MB limitdan katta chiqdi "
            f"(video bitreyti juda notekis) - qo'lda kichikroq qismlarga bo'lib yuklang."
        )
    return parts


def mux_video_audio(video_path: Path, audio_path: Path, out_path: Path):
    """Original videoning tasvirini saqlab, audio yo'lini yangi audio bilan almashtiradi."""
    cmd = [
        ffmpeg_exe(), "-y", "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg video+audio birlashtirishda {FFMPEG_TIMEOUT // 60} daqiqadan ortiq davom etdi va "
            f"to'xtatildi. Qayta urinib ko'ring."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi (video+audio birlashtirish): {(proc.stdout or '')[-2000:]}")
    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise RuntimeError("Yakuniy video fayli yaratilmadi yoki bo'sh.")
    try:
        probe = subprocess.run([ffmpeg_exe(), "-i", str(out_path)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="ignore",
                                timeout=FFMPEG_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Yakuniy videoni tekshirishda ffmpeg javob bermadi. Qayta urinib ko'ring.")
    if "Audio:" not in (probe.stdout or ""):
        raise RuntimeError(
            "Yakuniy videoda audio trek topilmadi. Audio manba fayli buzuq yoki bo'sh bo'lishi mumkin - "
            "'Audio' bosqichida audio faylni tekshirib, kerak bo'lsa qayta yarating."
        )


def _run_ffmpeg(cmd: list, description: str):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg ({description}) {FFMPEG_TIMEOUT // 60} daqiqadan ortiq davom etdi va to'xtatildi. "
            f"Qayta urinib ko'ring."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi ({description}): {(proc.stdout or '')[-1500:]}")


def _detect_fps(ffmpeg_info: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", ffmpeg_info or "")
    return float(m.group(1)) if m else 25.0


def mux_video_audio_with_freezes(video_path: Path, audio_path: Path, out_path: Path,
                                  freeze_points: list, work_dir: Path):
    """E-band: agar audio ba'zi joylarda o'ziga ajratilgan vaqtdan uzunroq chiqqan
    bo'lsa (freeze_points), yakuniy videoda o'sha nuqtalarda kadr bir necha
    soniya 'muzlab' turadi (video to'xtaydi, audio davom etadi) - shunda hech
    qanday overlap yoki audio yo'qolishi bo'lmaydi. freeze_points bo'sh bo'lsa,
    oddiy (tez, qayta kodlanmaydigan) mux ishlatiladi."""
    freeze_points = [f for f in (freeze_points or []) if f.get("duration", 0) > 0.05]
    if not freeze_points:
        mux_video_audio(video_path, audio_path, out_path)
        return

    work_dir.mkdir(parents=True, exist_ok=True)
    freeze_points = sorted(freeze_points, key=lambda f: f["time"])

    try:
        probe = subprocess.run([ffmpeg_exe(), "-i", str(video_path)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="ignore",
                                timeout=FFMPEG_PROBE_TIMEOUT)
        fps = _detect_fps(probe.stdout)
    except subprocess.TimeoutExpired:
        fps = 25.0
    total_duration = get_duration_seconds(video_path)

    part_paths = []
    prev_time = 0.0
    for i, fp in enumerate(freeze_points):
        t = min(fp["time"], total_duration)
        dur = fp["duration"]
        if t > prev_time:
            trim_path = work_dir / f"part_{i:03d}_trim.mp4"
            _run_ffmpeg([
                ffmpeg_exe(), "-y", "-ss", str(prev_time), "-to", str(t), "-i", str(video_path),
                "-an", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                str(trim_path),
            ], f"{i + 1}-qism (oddiy)")
            part_paths.append(trim_path)

        frame_path = work_dir / f"freeze_{i:03d}.jpg"
        _run_ffmpeg([
            ffmpeg_exe(), "-y", "-ss", str(t), "-i", str(video_path), "-vframes", "1", str(frame_path),
        ], f"{i + 1}-qism (kadr olish)")
        freeze_path = work_dir / f"part_{i:03d}_freeze.mp4"
        _run_ffmpeg([
            ffmpeg_exe(), "-y", "-loop", "1", "-i", str(frame_path), "-t", str(dur),
            "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(freeze_path),
        ], f"{i + 1}-qism (muzlatish)")
        part_paths.append(freeze_path)
        prev_time = t

    if prev_time < total_duration:
        tail_path = work_dir / "part_zzz_tail.mp4"
        _run_ffmpeg([
            ffmpeg_exe(), "-y", "-ss", str(prev_time), "-i", str(video_path),
            "-an", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(tail_path),
        ], "oxirgi qism")
        part_paths.append(tail_path)

    concat_list_path = work_dir / "concat_list.txt"
    concat_list_path.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in part_paths), encoding="utf-8"
    )
    video_only_path = work_dir / "video_only.mp4"
    _run_ffmpeg([
        ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
        "-c", "copy", str(video_only_path),
    ], "bo'laklarni birlashtirish")

    mux_video_audio(video_only_path, audio_path, out_path)

    for p in part_paths:
        p.unlink(missing_ok=True)
    for f in work_dir.glob("freeze_*.jpg"):
        f.unlink(missing_ok=True)
    concat_list_path.unlink(missing_ok=True)
    video_only_path.unlink(missing_ok=True)


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?…])\s+(?=[A-ZА-ЯЁ0-9"\'\(])|\n\s*\n')


def split_plain_text_into_segments(text: str, start_time: float, end_time: float) -> list:
    """Vaqt belgisi yo'q, uzluksiz oddiy matnni (masalan .txt fayldan yuklanganda) gaplarga
    bo'lib, berilgan vaqt oralig'iga (start_time..end_time) matn uzunligiga mutanosib
    ravishda taqsimlaydi. SRT'dagi kabi aniq vaqt bermaydi (faqat taxminiy, tekis
    taqsimlangan), lekin bitta ulkan blok o'rniga subtitr va bo'lak-darajasidagi
    tahrirlash uchun foydali kichikroq bo'laklar beradi."""
    normalized = (text or "").strip()
    if not normalized:
        return []

    pieces = []
    for para in re.split(r"\n\s*\n", normalized):
        para = para.strip()
        if not para:
            continue
        for s in _SENTENCE_SPLIT_RE.split(para):
            s = s.strip()
            if s:
                pieces.append(s)
    if not pieces:
        return []
    if len(pieces) == 1:
        return [{"start": start_time, "end": end_time, "text": pieces[0]}]

    total_chars = sum(len(p) for p in pieces) or 1
    total_duration = max(end_time - start_time, 0.1)
    segments = []
    cursor = start_time
    for i, p in enumerate(pieces):
        is_last = i == len(pieces) - 1
        seg_end = end_time if is_last else min(cursor + total_duration * (len(p) / total_chars), end_time)
        segments.append({"start": round(cursor, 3), "end": round(seg_end, 3), "text": p})
        cursor = seg_end
    return segments


def extract_audio_slice(input_path: Path, start: float, end: float, out_path: Path, pad: float = 0.3):
    """Original videodan bitta segmentga mos kichik audio bo'lakchasini ajratib oladi -
    foydalanuvchi bitta segmentni qayta Whisper'ga yuborib, matnini yangilamoqchi bo'lganda
    ishlatiladi (butun 5 daqiqalik bo'lakni qayta yubormasdan). `pad` - chegaralarda so'z
    kesilib qolmasligi uchun ozgina xavfsizlik zaxirasi (soniya)."""
    s = max(start - pad, 0)
    duration = max(end - start + 2 * pad, 0.3)
    cmd = [
        ffmpeg_exe(), "-y", "-ss", str(s), "-i", str(input_path), "-t", str(duration),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg segment audio ajratishda juda uzoq davom etdi va to'xtatildi. Qayta urinib ko'ring.")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi (segment audio ajratish): {(proc.stdout or '')[-1500:]}")
    if not out_path.exists() or out_path.stat().st_size < 100:
        raise RuntimeError("Segment uchun audio ajratilmadi (fayl bo'sh chiqdi).")


def extract_and_chunk(input_path: Path, work_dir: Path, chunk_seconds: int):
    """Videoni audioga aylantiradi va belgilangan uzunlikdagi bo'laklarga bo'ladi.
    Faqat preprocessing bosqichida chaqiriladi, OpenAI'ga hech narsa yubormaydi."""
    work_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(work_dir / "chunk_%05d.mp3")
    cmd = [
        ffmpeg_exe(), "-y", "-i", str(input_path),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
        "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
        pattern,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg audio ajratib bo'laklashda juda uzoq davom etdi va to'xtatildi. Qayta urinib ko'ring.")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi: {(proc.stdout or '')[-2000:]}")

    chunk_files = sorted(work_dir.glob("chunk_*.mp3"))
    if not chunk_files:
        raise RuntimeError("Ovoz bo'laklarga bo'linmadi (video ichida audio topilmadimi?).")

    chunks = []
    cumulative = 0.0
    for cf in chunk_files:
        dur = get_duration_seconds(cf)
        chunks.append({"path": cf, "start": cumulative, "end": cumulative + dur})
        cumulative += dur
    return chunks


# ---------------------------------------------------------------------------
#                          LUG'AT (GLOSSARY) FUNKSIYALARI
# ---------------------------------------------------------------------------

def split_synonyms(raw: str):
    candidates = []
    paren_contents = re.findall(r"\(([^)]*)\)", raw)
    main = re.sub(r"\([^)]*\)", "", raw)
    for chunk in [main] + paren_contents:
        for part in re.split(r"[/,]", chunk):
            p = part.strip().strip(".").strip()
            if p:
                candidates.append(p)
    return candidates


def build_term_variants(lang_code: str, group: str = None):
    """Tarjimadan keyingi TUZATISH bosqichi uchun - group berilmasa, BARCHA
    atamalar ishlatiladi (bu bosqich hech qachon cheklanmaydi)."""
    variants = []
    seen = set()
    for entry in GLOSSARY:
        if group and group not in entry.get("groups", []):
            continue
        raw = entry.get(lang_code, "") or ""
        for phrase in split_synonyms(raw):
            if len(phrase) < 4:
                continue
            key = phrase.lower()
            if key in seen:
                continue
            seen.add(key)
            wc = len(re.split(r"[\s-]+", phrase))
            variants.append((key, phrase, wc))
    variants.sort(key=lambda v: -v[2])
    return variants


def build_initial_prompt(lang_code: str, max_chars: int = 900, group: str = None) -> str:
    """Whisper'ning 'maslahat' (prompt) maydoni uchun - OpenAI'da qattiq hajm
    cheklovi bor (~200 so'z), shuning uchun 'group' berilsa faqat shu
    yo'nalishga tegishli atamalar ishlatiladi (eng foydali natija uchun)."""
    terms = []
    seen = set()
    for entry in GLOSSARY:
        if group and group not in entry.get("groups", []):
            continue
        raw = entry.get(lang_code, "") or ""
        parts = split_synonyms(raw)
        first = parts[0] if parts else ""
        if first and first.lower() not in seen:
            seen.add(first.lower())
            terms.append(first)
    prompt = ", ".join(terms)
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars]
    return prompt


def correct_segment_with_glossary(text: str, variants) -> str:
    if not variants or not text.strip():
        return text
    tokens = re.findall(r"\w+(?:-\w+)*|[^\w\s]|\s+", text, flags=re.UNICODE)
    word_positions = [i for i, t in enumerate(tokens) if re.match(r"\w", t, flags=re.UNICODE)]

    consumed = set()
    for phrase_lower, canonical, wc in variants:
        if wc < 2 or wc > 4:
            continue
        n = len(word_positions)
        for start_idx in range(n - wc + 1):
            idxs = word_positions[start_idx:start_idx + wc]
            if any(i in consumed for i in idxs):
                continue
            window_text = "".join(tokens[idxs[0]:idxs[-1] + 1]).strip()
            window_lower = window_text.lower()
            if window_lower == phrase_lower:
                continue
            ratio = difflib.SequenceMatcher(None, window_lower, phrase_lower).ratio()
            if ratio >= GLOSSARY_MATCH_THRESHOLD:
                tokens[idxs[0]] = canonical
                for i in idxs[1:]:
                    tokens[i] = ""
                for i in idxs:
                    consumed.add(i)
    return "".join(tokens)


def correct_segments_with_glossary(segments: list, variants) -> list:
    """Butun video uchun barcha segmentlarni bitta chaqiruvda tuzatadi - sinxron,
    CPU bilan band funksiya (event loop'ni bloklamaslik uchun chaqiruvchi tomon
    buni alohida thread'da - run_in_executor orqali - ishga tushirishi kerak)."""
    final_segments = []
    for s in segments:
        text = correct_segment_with_glossary(s["text"], variants) if variants else s["text"]
        text = " ".join(text.split())
        if text:
            final_segments.append({"start": s["start"], "end": s["end"], "text": text})
    return final_segments


DEFAULT_WHISPER_INSTRUCTION = (
    "Bu stomatologiya/tibbiyot sohasidagi ma'ruza. Tibbiy va stomatologik "
    "atamalarni aniq va izchil yoz."
)


def build_prompt(language: str, instruction: str, group: str = None) -> str:
    base_instruction = (instruction or "").strip() or DEFAULT_WHISPER_INSTRUCTION
    remaining = max(900 - len(base_instruction) - 1, 0)
    if language == "ru":
        glossary_prompt = build_initial_prompt("ru", remaining, group=group)
    elif language == "en":
        glossary_prompt = build_initial_prompt("en", remaining, group=group)
    else:
        half = remaining // 2
        glossary_prompt = (build_initial_prompt("ru", half, group=group) + " " +
                            build_initial_prompt("en", half, group=group))
    return (base_instruction + " " + glossary_prompt).strip()


def variants_for_language(detected_lang: str):
    detected = (detected_lang or "").lower()
    if detected.startswith("ru"):
        return build_term_variants("ru")
    elif detected.startswith("en"):
        return build_term_variants("en")
    return []


# ---------------------------------------------------------------------------
#                          TAKRORLANISH (REPETITION) ANIQLASH
# ---------------------------------------------------------------------------

def detect_repetition(text: str, threshold: int = None):
    """So'z/ibora ketma-ket necha marta takrorlanganini tekshiradi.
    threshold marotabadan ko'p ketma-ket takrorlansa shubhali hisoblanadi.
    Qaytaradi: (is_suspicious: bool, matched_phrase: str|None)"""
    threshold = threshold if threshold is not None else REPETITION_THRESHOLD
    words = text.strip().split()
    if len(words) < 6:
        return False, None
    for win in (1, 2, 3):
        max_run = cur_run = 1
        run_phrase = None
        i = win
        while i < len(words):
            a = " ".join(words[i - win:i]).lower()
            b = " ".join(words[i:i + win]).lower()
            if a == b:
                cur_run += 1
                if cur_run > max_run:
                    max_run = cur_run
                    run_phrase = a
            else:
                cur_run = 1
            i += win
        if max_run > threshold:
            return True, run_phrase
    return False, None


_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_script_mismatch(text: str, expected_language: str) -> bool:
    """Whisper ba'zan uzun/notinch audio o'rtasida boshqa tilga 'sirg'alib'
    ketadi (hallucination) - masalan inglizcha boshlanib, ruscha davom etadi.
    Bu yozuv (skript) darajasida tekshiradi: kutilgan til lotin alifbosi
    (masalan inglizcha) bo'lsa-yu, segment ko'pincha kirillcha bo'lsa - yoki
    aksincha - shubhali deb belgilaydi."""
    expected = (expected_language or "").lower()
    if not expected or len(text) < 8:
        return False
    cyr = len(_CYRILLIC_RE.findall(text))
    lat = len(_LATIN_RE.findall(text))
    total = cyr + lat
    if total < 6:
        return False
    if expected.startswith("en") and cyr / total > 0.4:
        return True
    if expected.startswith("ru") and lat / total > 0.6:
        return True
    return False


def assess_segment_issues(whisper_segments: list, chunk_offset: float, expected_language: str = "") -> list:
    """Har bir Whisper segmenti uchun shubhali joylarni aniqlaydi (takrorlanish,
    sukut/musiqa/tushunarsiz audio, til chalkashishi). Jarayonni to'xtatmaydi -
    faqat belgilaydi."""
    issues = []
    for s in whisper_segments:
        text = (s.get("text") or "").strip()
        start = float(s.get("start", 0)) + chunk_offset
        end = float(s.get("end", 0)) + chunk_offset
        no_speech_prob = s.get("no_speech_prob")
        avg_logprob = s.get("avg_logprob")

        if no_speech_prob is not None and no_speech_prob > 0.6 and len(text) < 8:
            issues.append({
                "kind": "no_speech", "start": start, "end": end,
                "detail": "Nutq aniqlanmadi (sukut, musiqa yoki tushunarsiz audio bo'lishi mumkin).",
            })
        elif avg_logprob is not None and avg_logprob < -1.0:
            issues.append({
                "kind": "low_confidence", "start": start, "end": end,
                "detail": "Transkripsiya ishonchliligi past (audio sifati yomon bo'lishi mumkin).",
            })

        suspicious, phrase = detect_repetition(text)
        if suspicious:
            issues.append({
                "kind": "repetition", "start": start, "end": end,
                "detail": f"Takrorlanish ehtimoli: \u201c{phrase}\u201d",
            })

        if detect_script_mismatch(text, expected_language):
            issues.append({
                "kind": "wrong_language", "start": start, "end": end,
                "detail": f"Kutilgan til ({expected_language}) bilan mos kelmaydi - Whisper boshqa tilga "
                          f"chalkashgan (hallucination) bo'lishi mumkin.",
            })
    return issues


def classify_chunk_error(exc: Exception) -> str:
    """Xom xato matnini foydalanuvchiga tushunarli sabab + tavsiya bilan qaytaradi."""
    msg = str(exc)
    if "timeout" in msg.lower() or isinstance(exc, (TimeoutError,)):
        return ("OpenAI Whisper javob berishga juda uzoq vaqt oldi (tarmoq yoki API sekinlashuvi). "
                "\"Qayta urinish\"ni bosing - odatda ikkinchi safar o'tadi.")
    if "connection" in msg.lower() or "connect" in msg.lower():
        return ("Serverdan OpenAI'ga ulanishda uzilish bo'ldi. Bir necha soniyadan keyin \"Qayta urinish\"ni bosing.")
    if "503" in msg or "502" in msg or "504" in msg:
        return "OpenAI serveri vaqtincha band (5xx xato). Bir necha daqiqadan keyin \"Qayta urinish\"ni bosing."
    if "429" in msg:
        return "So'rovlar chegarasiga yetildi (429). Boshqa API kalit qo'shing yoki biroz kutib qayta urinib ko'ring."
    if "401" in msg or "403" in msg:
        return "API kalit noto'g'ri yoki bekor qilingan. Sozlamalarda kalitni tekshiring."
    return f"Kutilmagan xato: {msg[:300]}. \"Qayta urinish\"ni bosing, davom etmasa API kalitni tekshiring."


# ---------------------------------------------------------------------------
#                          OPENAI WHISPER
# ---------------------------------------------------------------------------

async def transcribe_chunk_via_api(client: httpx.AsyncClient, chunk_path: Path, api_key: str,
                                    language: str, prompt: str) -> dict:
    with chunk_path.open("rb") as f:
        files = {"file": ("chunk.mp3", f, "audio/mpeg")}
        data = {"model": "whisper-1", "response_format": "verbose_json"}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt[:900]
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data=data, files=files,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI xatosi ({resp.status_code}): {resp.text[:600]}")
    return resp.json()


def estimate_whisper_cost(duration_seconds: float) -> float:
    # whisper-1 taxminiy narxi: $0.006 / daqiqa
    return round((duration_seconds / 60.0) * 0.006, 6)


def is_key_error(exc: Exception) -> bool:
    """401/403/429 kabi kalitga bog'liq xatolarni aniqlaydi (boshqa kalitga o'tish uchun)."""
    msg = str(exc)
    return any(code in msg for code in ("401", "403", "429"))


# ---------------------------------------------------------------------------
#                          SRT / TXT YARATISH
# ---------------------------------------------------------------------------

def fmt_srt_time(total_sec: float) -> str:
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = int(total_sec % 60)
    ms = int(round((total_sec - int(total_sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_minsec(total_sec: float) -> str:
    total_sec = int(round(total_sec))
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def build_srt(segments) -> str:
    lines = []
    for i, s in enumerate(segments, start=1):
        lines.append(f"{i}\n{fmt_srt_time(s['start'])} --> {fmt_srt_time(s['end'])}\n{s['text']}\n")
    return "\n".join(lines)


def build_txt(segments) -> str:
    parts = []
    prev_end = segments[0]["start"] if segments else 0
    for s in segments:
        gap = s["start"] - prev_end
        if gap >= 3.0 and prev_end > 0:
            parts.append(f"--- [pauza: {fmt_minsec(prev_end)} dan {fmt_minsec(s['start'])} gacha] ---")
        parts.append(f"[{fmt_minsec(s['start'])} - {fmt_minsec(s['end'])}] {s['text']}")
        prev_end = s["end"]
    return "\n\n".join(parts)


def fmt_vtt_time(total_sec: float) -> str:
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = int(total_sec % 60)
    ms = int(round((total_sec - int(total_sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def build_vtt(segments) -> str:
    """Brauzer <track> elementi uchun WebVTT format (SRT emas - subtitle
    kuydirilmaydi, alohida trek sifatida ishlatiladi)."""
    lines = ["WEBVTT", ""]
    for s in segments:
        lines.append(f"{fmt_vtt_time(s['start'])} --> {fmt_vtt_time(s['end'])}")
        lines.append(s["text"])
        lines.append("")
    return "\n".join(lines)


def srt_to_vtt(srt_text: str) -> str:
    """Mavjud SRT matnini WebVTT'ga o'giradi (vaqt formatidagi vergulni nuqtaga almashtiradi)."""
    body = re.sub(r"^\d+\s*$", "", srt_text, flags=re.MULTILINE)
    body = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return "WEBVTT\n\n" + body + "\n"
