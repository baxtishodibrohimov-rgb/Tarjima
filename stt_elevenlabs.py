"""ElevenLabs Scribe - matn olish provayderi (POST /v1/speech-to-text).

Parametrlar rasmiy ElevenLabs Python SDK'sidan (speech_to_text/raw_client.py) tekshirilgan:
  model_id, file, language_code, tag_audio_events, num_speakers (<= 32),
  timestamps_granularity ("word"), diarize, keyterms (<= 1000 ta, har biri < 50 belgi,
  <= 5 so'z, < > { } [ ] \\ belgilarsiz; +20% narx).
Javob: language_code, text, words[{text, start, end, type: word|spacing|audio_event,
  speaker_id, logprob}].
"""
import asyncio
import re
import subprocess
from pathlib import Path

import httpx

import transcription
from glossary_data import GLOSSARY
from timing_contract import ELEVENLABS_MAX_BYTES, ELEVENLABS_MAX_SEC

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = "scribe_v2"
# Narx: soatiga $0.22; keyterms bilan +20% (rasmiy hujjat bo'yicha).
PRICE_PER_HOUR = 0.22
KEYTERMS_SURCHARGE = 0.20
MAX_KEYTERMS = 1000
_BAD_KEYTERM_CHARS = re.compile(r"[<>{}\[\]\\]")

# Konteynerga ko'chiriladigan audio kodeklar (qayta kodlanmaydi - sifat bir xil, yuklash tez).
_COPY_CONTAINERS = {"aac": "m4a", "mp3": "mp3", "opus": "ogg", "vorbis": "ogg", "flac": "flac"}


def estimate_cost(duration_sec: float, with_keyterms: bool) -> float:
    rate = PRICE_PER_HOUR * (1 + (KEYTERMS_SURCHARGE if with_keyterms else 0))
    return round(duration_sec / 3600 * rate, 6)


def keyterms_for(language: str, group: str = None) -> list:
    """glossary_data dan tanlangan til (va yo'nalish) atamalari, ElevenLabs cheklovlari bilan."""
    lang = (language or "ru")[:2]
    terms, seen = [], set()
    for entry in GLOSSARY:
        if group and group not in entry.get("groups", []):
            continue
        for phrase in transcription.split_synonyms(entry.get(lang, "") or ""):
            phrase = _BAD_KEYTERM_CHARS.sub("", phrase).strip()
            if not phrase or len(phrase) >= 50 or len(phrase.split()) > 5 or phrase.lower() in seen:
                continue
            seen.add(phrase.lower())
            terms.append(phrase)
            if len(terms) >= MAX_KEYTERMS:
                return terms
    return terms


def audio_codec(path: Path) -> str:
    proc = subprocess.run([transcription.ffmpeg_exe(), "-hide_banner", "-i", str(path)], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="ignore",
                          timeout=transcription.FFMPEG_PROBE_TIMEOUT)
    m = re.search(r"Audio: (\w+)", proc.stdout or "")
    return m.group(1).lower() if m else ""


def extract_audio(video_path: Path, work_dir: Path) -> Path:
    """Videodan audio yo'lini QAYTA KODLAMASDAN ajratadi (-vn -c:a copy). Kodek
    mos konteynerga sig'masa - flac (yo'qotishsiz)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    ext = _COPY_CONTAINERS.get(audio_codec(video_path))
    if ext:
        out = work_dir / f"stt_audio.{ext}"
        proc = subprocess.run([transcription.ffmpeg_exe(), "-y", "-i", str(video_path), "-vn", "-c:a", "copy",
                               str(out)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="ignore", timeout=transcription.FFMPEG_TIMEOUT)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 1024:
            return out
        out.unlink(missing_ok=True)
    out = work_dir / "stt_audio.flac"
    transcription._run_ffmpeg([transcription.ffmpeg_exe(), "-y", "-i", str(video_path), "-vn", "-ac", "1",
                               "-c:a", "flac", str(out)], "audio ajratish (flac)")
    return out


def plan_parts(duration: float, size: int, silences: list) -> list:
    """Fayl chegaradan (3 GB / 10 soat) katta bo'lsagina - kerakli eng kam
    qismga, eng yaqin jimlikda bo'linadi. Qaytaradi: [(boshi, oxiri), ...]."""
    parts_needed = max(int(size // ELEVENLABS_MAX_BYTES) + 1, int(duration // ELEVENLABS_MAX_SEC) + 1)
    if parts_needed <= 1:
        return [(0.0, duration)]
    target = duration / parts_needed
    return transcription.plan_chunk_ranges(duration, silences, target, window=min(120.0, target / 4), overlap=2.0)


def cut_part(audio_path: Path, start: float, end: float, out: Path) -> Path:
    transcription._run_ffmpeg([transcription.ffmpeg_exe(), "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
                               "-i", str(audio_path), "-c:a", "copy", str(out)], "audio qismini kesish")
    return out


def build_form(language: str, diarize: bool, num_speakers: int = None, keyterms: list = None) -> dict:
    data = {"model_id": MODEL_ID, "timestamps_granularity": "word", "tag_audio_events": "true",
            "diarize": "true" if diarize else "false"}
    if language:
        data["language_code"] = language
    if diarize and num_speakers:
        data["num_speakers"] = str(int(num_speakers))
    if keyterms:
        data["keyterms"] = list(keyterms)
    return data


async def transcribe(path: Path, api_key: str, form: dict, attempts: int = 3) -> dict:
    """Bitta faylni yuboradi (uzun audio uchun 60+ daqiqa timeout, 3 martagacha qayta urinish)."""
    last_err = None
    timeout = httpx.Timeout(4 * 3600, connect=60)
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                with path.open("rb") as f:
                    resp = await client.post(API_URL, headers={"xi-api-key": api_key}, data=form,
                                             files={"file": (path.name, f, "application/octet-stream")})
            if resp.status_code in (401, 403):
                raise PermissionError(f"ElevenLabs kaliti rad etildi ({resp.status_code}): {resp.text[:300]}")
            if resp.status_code == 422 or resp.status_code == 400:
                raise ValueError(f"ElevenLabs so'rovni qabul qilmadi ({resp.status_code}): {resp.text[:500]}")
            if resp.status_code >= 400:
                raise RuntimeError(f"ElevenLabs xatosi ({resp.status_code}): {resp.text[:500]}")
            return resp.json()
        except (PermissionError, ValueError):
            raise
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                await asyncio.sleep(10 * (attempt + 1))
    raise RuntimeError(f"ElevenLabs'ga yuborib bo'lmadi: {last_err}")


def response_words(data: dict, offset: float, speaker_ids: dict) -> list:
    """Javobdagi type == "word" so'zlar -> {"w","s","e","spk","lp"}. Spikerlar
    birinchi paydo bo'lish tartibida 1, 2, 3 ... (speaker_ids barcha qismlar uchun umumiy).
    audio_event (kulgi, musiqa) subtitrga kirmaydi - pauza bo'lib qoladi."""
    words = []
    for w in data.get("words") or []:
        if w.get("type", "word") != "word" or not (w.get("text") or "").strip():
            continue
        spk = None
        if w.get("speaker_id") is not None:
            spk = speaker_ids.setdefault(w["speaker_id"], len(speaker_ids) + 1)
        words.append({"w": w["text"].strip(), "s": round(float(w.get("start") or 0) + offset, 3),
                      "e": round(float(w.get("end") or w.get("start") or 0) + offset, 3), "spk": spk,
                      "lp": w.get("logprob")})
    return words
