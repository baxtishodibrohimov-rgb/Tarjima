"""
Learning intro: faqat words_json (translation.parse_learning_srt) asosida.

Tarkib (4.1-4.2): "Takrorlash: N ta so'z" sarlavhasi (2 s) -> takror so'zlar
ekranlari (8 tadan, 2x4, ovozsiz) -> "Yangi so'zlar: N ta" sarlavhasi (2 s) ->
har yangi so'z uchun kartochka: 0.5 s jim + ORIGINAL + 0.6 s + O'ZBEKCHA + 0.6 s
+ ORIGINAL takror + 1.2 s jim. Kartochka uzunligi haqiqiy audio uzunligidan.

Intro Learning videosining parametrlari (o'lcham, fps, pikselformat, kodek,
audio oqimlar soni/chastota/kanal) bilan aynan bir xil render qilinadi.
"""
import asyncio
import json
import math
import shutil
import subprocess
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, features

import database as db
import keys_manager
import learning
import transcription
import translation
import tts

TITLE_SECONDS = 2.0
REPEAT_PER_SCREEN = 8
REPEAT_FULL_SECONDS = 12.0
CARD_LEAD, CARD_GAP, CARD_TAIL = 0.5, 0.6, 1.2
DEFAULT_OPENAI_VOICE = "alloy"

INSTRUCTIONS = {
    "ru": ("Speak Russian. Pronounce only this single Russian word clearly and naturally, with correct "
           "stress, like a native Russian speaker. Do not add anything else."),
    "en": ("Speak English. Pronounce only this single English word clearly and naturally, like a native "
           "English speaker. Do not add anything else."),
    "uz": ("Speak Uzbek. Pronounce only this Uzbek word or short meaning clearly and naturally, like a native "
           "Uzbek speaker. Do not add anything else."),
}

BG = (17, 19, 26)
YELLOW = (255, 212, 0)
WHITE = (255, 255, 255)
MUTED = (165, 170, 185)


# ---------------------------------------------------------------------------
#                            TARKIB VA VAQTLAR
# ---------------------------------------------------------------------------

def repeat_screen_seconds(n: int) -> float:
    if n >= REPEAT_PER_SCREEN:
        return REPEAT_FULL_SECONDS
    return max(6.0, 3.0 + 1.2 * n)


def card_seconds(original_seconds: float, uzbek_seconds: float) -> float:
    return CARD_LEAD + original_seconds + CARD_GAP + uzbek_seconds + CARD_GAP + original_seconds + CARD_TAIL


def plan_intro(word_lists: dict) -> list:
    """Ekranlar ro'yxati; kartochka uzunligi keyin audio bilan aniqlanadi (seconds=None)."""
    plan = []
    repeat, new = word_lists["repeat"], word_lists["new"]
    if repeat:
        plan.append({"kind": "title", "text": f"Takrorlash: {len(repeat)} ta so‘z", "seconds": TITLE_SECONDS})
        pages = [repeat[i:i + REPEAT_PER_SCREEN] for i in range(0, len(repeat), REPEAT_PER_SCREEN)]
        for p, words in enumerate(pages):
            plan.append({"kind": "repeat", "words": words, "page": p + 1, "pages": len(pages),
                         "seconds": repeat_screen_seconds(len(words))})
    if new:
        plan.append({"kind": "title", "text": f"Yangi so‘zlar: {len(new)} ta", "seconds": TITLE_SECONDS})
        for i, w in enumerate(new):
            plan.append({"kind": "card", "word": w, "number": i + 1, "total": len(new), "seconds": None})
    return plan


def is_cyrillic(s: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in s)


def original_tts_text(lemma: str, strip_stress: bool) -> str:
    return lemma.replace("́", "") if strip_stress else lemma


# ---------------------------------------------------------------------------
#                                SLAYDLAR
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = learning.FONTS_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    engine = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
    return ImageFont.truetype(str(path), max(size, 8), layout_engine=engine)


def _fit(draw, text: str, max_width: float, size: int, bold: bool = False):
    font = _font(size, bold)
    while size > 12 and draw.textlength(text, font=font) > max_width:
        size = int(size * 0.92)
        font = _font(size, bold)
    return font


def _center(draw, text: str, cx: float, cy: float, font, fill):
    draw.text((cx, cy), text, font=font, fill=fill, anchor="mm")


def render_slide(item: dict, width: int, height: int) -> Image.Image:
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    if item["kind"] == "title":
        font = _fit(d, item["text"], width * 0.85, int(height * 0.09), bold=True)
        _center(d, item["text"], width / 2, height / 2, font, WHITE)
    elif item["kind"] == "repeat":
        head = "Takrorlash" + (f"  {item['page']}/{item['pages']}" if item["pages"] > 1 else "")
        _center(d, head, width / 2, height * 0.09, _fit(d, head, width * 0.8, int(height * 0.06), True), MUTED)
        cols, rows = 2, 4
        top, bottom = height * 0.17, height * 0.95
        cell_w, cell_h = width / cols, (bottom - top) / rows
        for i, w in enumerate(item["words"]):
            col, row = i // rows, i % rows
            cx = cell_w * col + cell_w / 2
            cy = top + cell_h * row + cell_h / 2
            lemma_font = _fit(d, w["lemma"], cell_w * 0.9, int(height * 0.06), True)
            meaning_font = _fit(d, w["meaning"], cell_w * 0.9, int(height * 0.042))
            _center(d, w["lemma"], cx, cy - cell_h * 0.17, lemma_font, WHITE)
            _center(d, w["meaning"], cx, cy + cell_h * 0.2, meaning_font, MUTED)
    else:
        w = item["word"]
        counter = f"Yangi so‘z {item['number']}/{item['total']}"
        _center(d, counter, width / 2, height * 0.1, _font(int(height * 0.045)), MUTED)
        _center(d, w["lemma"], width / 2, height * 0.43,
                _fit(d, w["lemma"], width * 0.88, int(height * 0.15), True), YELLOW)
        _center(d, w["meaning"], width / 2, height * 0.66,
                _fit(d, w["meaning"], width * 0.88, int(height * 0.085)), WHITE)
    return img


# ---------------------------------------------------------------------------
#                                 OVOZ
# ---------------------------------------------------------------------------

async def _cached_tts(provider: str, text: str, params: dict, generate):
    """tts.cache_key_for / tts.cache_path keshi - keyingi videolarda qayta pul sarflanmaydi.
    Qaytaradi: (yo'l, keshdanmi)."""
    path = tts.cache_path(tts.cache_key_for(provider, text, **params), "wav")
    if path.exists() and path.stat().st_size > 0:
        return path, True
    last_err = None
    for attempt in range(3):
        try:
            data = await generate()
            path.write_bytes(data)
            return path, False
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    raise last_err


async def synthesize_new_words(new_words: list, video_id: str, owner_id: str, voice: str,
                               strip_stress: bool, on_progress=None) -> list:
    """Har yangi so'z uchun (original_wav, ozbekcha_wav).

    Ikkala ovoz ham foydalanuvchining OpenAI kaliti bilan avtomatik yaratiladi:
    original lemma kirill bo'lsa ruscha, lotin bo'lsa inglizcha; ma'no esa
    o'zbekcha ko'rsatma bilan o'qiladi. Alohida Aisha kaliti talab qilinmaydi.
    """
    if not new_words:
        return []
    voice = voice or DEFAULT_OPENAI_VOICE
    results = []
    async with httpx.AsyncClient(timeout=120) as client:
        for i, w in enumerate(new_words):
            text = original_tts_text(w["lemma"], strip_stress)
            instructions = INSTRUCTIONS["ru" if is_cyrillic(w["lemma"]) else "en"]
            used = {}

            async def gen_original():
                kid, raw = keys_manager.get_next_active_key(owner_id=owner_id)
                if not raw:
                    raise RuntimeError("Ishlaydigan OpenAI API kalit topilmadi.")
                used["kid"] = kid
                try:
                    data = await tts.openai_tts_generate_one(client, text, voice, raw, instructions, speed=1.0)
                except Exception as e:
                    keys_manager.mark_result(kid, False, str(e)[:500])
                    raise
                keys_manager.mark_result(kid, True)
                return data

            orig_path, orig_cached = await _cached_tts(
                "openai", text, {"voice": voice, "instructions": instructions, "speed": 1.0}, gen_original)
            if not orig_cached:
                db.add_cost(video_id, "tts_openai", round((len(text) / 1000) * 0.015, 6),
                            detail=f"Learning intro, OpenAI TTS: {text}", owner_id=owner_id)

            meaning = w["meaning"]
            uz_path, uz_cached = await _cached_tts(
                "openai", meaning, {"voice": voice, "instructions": INSTRUCTIONS["uz"], "speed": 1.0},
                lambda: _openai_intro_tts(client, meaning, voice, INSTRUCTIONS["uz"], owner_id))
            if not uz_cached:
                db.add_cost(video_id, "tts_openai", round((len(meaning) / 1000) * 0.015, 6),
                            detail=f"Learning intro, OpenAI TTS (uz): {meaning}", owner_id=owner_id)
            results.append((orig_path, uz_path))
            if on_progress:
                on_progress(i + 1, len(new_words))
    return results


async def _openai_intro_tts(client, text: str, voice: str, instructions: str, owner_id: str) -> bytes:
    """Intro ma'nosi uchun OpenAI kalit aylantirish va natijani qayd etish."""
    kid, raw = keys_manager.get_next_active_key(owner_id=owner_id)
    if not raw:
        raise RuntimeError("Ishlaydigan OpenAI API kalit topilmadi.")
    try:
        data = await tts.openai_tts_generate_one(client, text, voice, raw, instructions, speed=1.0)
    except Exception as exc:
        keys_manager.mark_result(kid, False, str(exc)[:500])
        raise
    keys_manager.mark_result(kid, True)
    return data


def decode_pcm(path: Path, sample_rate: int, channels: int) -> bytes:
    proc = subprocess.run([transcription.ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "s16le",
                           "-acodec", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels), "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=transcription.FFMPEG_PROBE_TIMEOUT)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"Audio faylni o'qib bo'lmadi ({path.name}): {proc.stderr.decode(errors='ignore')[-300:]}")
    return proc.stdout


# ---------------------------------------------------------------------------
#                                YIG'ISH
# ---------------------------------------------------------------------------

def _run(cmd: list, what: str, cwd: Path = None):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                          timeout=transcription.FFMPEG_TIMEOUT, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi ({what}): {(proc.stdout or '')[-1200:]}")


def _encoder_available(name: str) -> bool:
    proc = subprocess.run([transcription.ffmpeg_exe(), "-hide_banner", "-encoders"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="ignore",
                          timeout=transcription.FFMPEG_PROBE_TIMEOUT)
    return f" {name} " in (proc.stdout or "")


def build_intro_video(plan: list, audio_files: list, info: dict, work_dir: Path, out_path: Path,
                      slides_dir: Path) -> dict:
    """Rejani (kartochkalar uchun audio bilan) intro videoga aylantiradi.
    Qaytaradi: {"duration": .., "slides": [{"file", "kind", "seconds"}]}."""
    work_dir.mkdir(parents=True, exist_ok=True)
    if slides_dir.exists():
        shutil.rmtree(slides_dir)
    slides_dir.mkdir(parents=True)
    exe = transcription.ffmpeg_exe()
    width, height, fps = info["width"], info["height"], info["fps"]
    fps_f = learning.fps_float(fps)
    n_audio = len(info["audio"])
    sample_rate = info["audio"][0]["sample_rate"] if n_audio else 48000
    channels = info["audio"][0]["channels"] if n_audio else 2
    frame_bytes = 2 * channels

    def silence(sec: float) -> bytes:
        return b"\x00" * (int(round(sec * sample_rate)) * frame_bytes)

    encoder = learning.video_encoder_for(info)
    if encoder != "libx264" and not _encoder_available(encoder):
        encoder = "libx264"
    venc = ["-c:v", encoder, "-pix_fmt", info["pix_fmt"]]
    if encoder == "libx264":
        venc += ["-preset", "veryfast", "-crf", "18", "-tune", "stillimage"]
    elif encoder == "libx265":
        venc += ["-preset", "veryfast", "-crf", "20"]
    timescale = ["-video_track_timescale", str(info["tbn"])] if info.get("tbn") else []

    clips, pcm, slides, total = [], bytearray(), [], 0.0
    card_i = 0
    for n, item in enumerate(plan, start=1):
        if item["kind"] == "card":
            orig_path, uz_path = audio_files[card_i]
            card_i += 1
            orig = decode_pcm(orig_path, sample_rate, channels)
            uz = decode_pcm(uz_path, sample_rate, channels)
            seg_pcm = (silence(CARD_LEAD) + orig + silence(CARD_GAP) + uz + silence(CARD_GAP) + orig
                       + silence(CARD_TAIL))
            seconds = len(seg_pcm) / (sample_rate * frame_bytes)
            frames = math.ceil(seconds * fps_f - 1e-6)
        else:
            seg_pcm = b""
            seconds = item["seconds"]
            frames = round(seconds * fps_f)
        seg_len = frames / fps_f
        target_bytes = int(round(seg_len * sample_rate)) * frame_bytes
        seg_pcm = seg_pcm[:target_bytes] + b"\x00" * max(target_bytes - len(seg_pcm), 0)
        pcm += seg_pcm
        total += seg_len

        slide_name = f"{n:02d}.png"
        slide_path = slides_dir / slide_name
        render_slide(item, width, height).save(slide_path, "PNG")
        slides.append({"file": slide_name, "kind": item["kind"], "seconds": round(seg_len, 3)})

        clip = work_dir / f"clip_{n:03d}.mp4"
        _run([exe, "-y", "-loop", "1", "-framerate", str(fps), "-i", str(slide_path), "-frames:v", str(frames),
              *venc, "-r", str(fps), *timescale, "-an", str(clip)], f"{n}-slayd")
        clips.append(clip)

    concat_list = work_dir / "clips.txt"
    concat_list.write_text("".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8")
    video_only = work_dir / "intro_video_only.mp4"
    _run([exe, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", *timescale,
          str(video_only)], "slaydlarni ulash")

    tmp_out = out_path.with_suffix(".rendering.mp4")
    if n_audio:
        pcm_path = work_dir / "intro_audio.pcm"
        pcm_path.write_bytes(bytes(pcm))
        maps = ["-map", "0:v:0"] + ["-map", "1:a:0"] * n_audio
        encoder_for_audio = {"aac": "aac", "mp3": "libmp3lame", "opus": "libopus"}
        aenc = ["-c:a", encoder_for_audio.get(info["audio"][0]["codec"], "aac"), "-b:a", "192k"]
        _run([exe, "-y", "-i", str(video_only), "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels),
              "-i", str(pcm_path), *maps, "-c:v", "copy", *aenc, "-ar", str(sample_rate), "-ac", str(channels),
              *timescale, "-t", f"{total:.3f}", "-movflags", "+faststart", str(tmp_out)], "intro ovozi")
    else:
        shutil.copyfile(video_only, tmp_out)
    tmp_out.replace(out_path)
    shutil.rmtree(work_dir, ignore_errors=True)
    return {"duration": round(transcription.get_duration_seconds(out_path) or total, 3), "slides": slides}


async def create_intro(blocks: list, clean_video: Path, out_path: Path, slides_dir: Path, work_dir: Path,
                       video_id: str, owner_id: str, voice: str, strip_stress: bool,
                       on_progress=None) -> dict:
    word_lists = translation.learning_word_lists(blocks)
    if not word_lists["new"] and not word_lists["repeat"]:
        raise ValueError("Intro yaratilmaydi: Learning SRT'da yangi ham, takror ham so'z yo'q.")
    plan = plan_intro(word_lists)
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, learning.probe_media, clean_video)

    def tts_progress(done, total):
        if on_progress:
            on_progress(5 + 60 * done / total, f"Ovoz: {done}/{total} yangi so'z")

    if on_progress:
        on_progress(5, "Ovozlar tayyorlanmoqda")
    audio_files = await synthesize_new_words(word_lists["new"], video_id, owner_id, voice,
                                             strip_stress, tts_progress)
    if on_progress:
        on_progress(70, "Slaydlar va intro video yig'ilmoqda")
    result = await loop.run_in_executor(None, build_intro_video, plan, audio_files, info, work_dir, out_path,
                                        slides_dir)
    result["slides_json"] = json.dumps(result["slides"], ensure_ascii=False)
    return result
