"""OpenAI bilan spikerlarni ajratish (whisper-1 spikerlarni ajratmaydi).

Rasmiy openai-python SDK (types/audio/transcription_create_params.py) bo'yicha:
  model=gpt-4o-transcribe-diarize, response_format=diarized_json, chunking_strategy=auto
  (30 s dan uzun audio uchun majburiy), known_speaker_names[] + known_speaker_references[]
  (data URL, har namuna 2-10 s, ko'pi bilan 4 ta spiker).
Javob: segments[{start, end, speaker, text}] - spiker "A", "B" ... yoki berilgan nom.

Har bir whisper so'ziga vaqt bo'yicha eng ko'p ustma-ust tushgan diarize segmentining
spikeri beriladi. Bo'laklar orasida spikerlar 1-bo'lakdan olingan namunalar bilan moslanadi.
"""
import base64
import subprocess
from pathlib import Path

import httpx

import transcription

MODEL = "gpt-4o-transcribe-diarize"
MAX_KNOWN_SPEAKERS = 4
PRICE_PER_MIN = 0.006


def estimate_cost(duration_sec: float) -> float:
    return round(duration_sec / 60 * PRICE_PER_MIN, 6)


async def diarize_file(client: httpx.AsyncClient, path: Path, api_key: str, language: str = "",
                       known: list = None) -> list:
    """Bitta audio faylni yuboradi. known: [(nom, data_url), ...]. Qaytaradi: segmentlar."""
    data = {"model": MODEL, "response_format": "diarized_json", "chunking_strategy": "auto"}
    if language:
        data["language"] = language
    if known:
        data["known_speaker_names[]"] = [name for name, _ in known[:MAX_KNOWN_SPEAKERS]]
        data["known_speaker_references[]"] = [ref for _, ref in known[:MAX_KNOWN_SPEAKERS]]
    with path.open("rb") as f:
        resp = await client.post("https://api.openai.com/v1/audio/transcriptions",
                                 headers={"Authorization": f"Bearer {api_key}"}, data=data,
                                 files={"file": (path.name, f, "audio/mpeg")})
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI diarize xatosi ({resp.status_code}): {resp.text[:400]}")
    return resp.json().get("segments") or []


def speaker_reference(audio_path: Path, start: float, end: float, work_dir: Path, name: str) -> str:
    """Spiker namunasi (2-10 s) - data URL (audio/mpeg, base64)."""
    duration = min(max(end - start, 2.0), 10.0)
    out = work_dir / f"ref_{name}.mp3"
    transcription._run_ffmpeg([transcription.ffmpeg_exe(), "-y", "-ss", f"{max(start, 0):.3f}", "-t",
                               f"{duration:.3f}", "-i", str(audio_path), "-ac", "1", "-ar", "16000", "-b:a", "64k",
                               str(out)], "spiker namunasi")
    data = base64.b64encode(out.read_bytes()).decode()
    out.unlink(missing_ok=True)
    return f"data:audio/mpeg;base64,{data}"


def pick_references(segments: list) -> dict:
    """Har spiker uchun eng uzun segment (namuna uchun) - {spiker: (start, end)}."""
    best = {}
    for s in segments:
        length = float(s["end"]) - float(s["start"])
        if length >= 2.0 and (s["speaker"] not in best or length > best[s["speaker"]][1] - best[s["speaker"]][0]):
            best[s["speaker"]] = (float(s["start"]), float(s["end"]))
    return best


def assign_speakers(words: list, segments: list) -> list:
    """Har so'zga eng ko'p ustma-ust tushgan segment spikeri (yo'q bo'lsa eng yaqini).
    segments: [{"start","end","speaker"}] (absolut vaqt, speaker - raqam)."""
    if not segments:
        return words
    segments = sorted(segments, key=lambda s: s["start"])
    out = []
    for w in words:
        best, best_overlap, best_dist = None, 0.0, float("inf")
        for s in segments:
            if s["start"] > w["e"] + 30:
                break
            overlap = min(w["e"], s["end"]) - max(w["s"], s["start"])
            if overlap > best_overlap:
                best, best_overlap = s, overlap
            elif best_overlap <= 0:
                dist = max(s["start"] - w["e"], w["s"] - s["end"], 0)
                if dist < best_dist:
                    best, best_dist = s, dist
        out.append({**w, "spk": best["speaker"] if best else None})
    return out


class SpeakerNumbers:
    """Spiker nomlari ("A", "spk1" ...) -> 1, 2, 3 ... (birinchi paydo bo'lish tartibida)."""

    def __init__(self):
        self.ids = {}

    def number(self, key) -> int:
        return self.ids.setdefault(key, len(self.ids) + 1)


async def diarize_chunks(chunks: list, api_key: str, language: str, work_dir: Path, log=None):
    """chunks: [{"path", "start_time"}] tartibda. Qaytaradi: (absolut segmentlar
    [{"start","end","speaker": raqam}], ishlangan soniyalar, spikerlar soni > 4 mi)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    numbers = SpeakerNumbers()
    known, name_to_number = [], {}
    result, seconds, too_many = [], 0.0, False
    async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=60)) as client:
        for i, chunk in enumerate(chunks):
            path = Path(chunk["path"])
            segs = await diarize_file(client, path, api_key, language, known if i > 0 else None)
            seconds += transcription.get_duration_seconds(path)
            offset = float(chunk["start_time"])
            if i == 0:
                refs = pick_references(segs)
                too_many = len(refs) > MAX_KNOWN_SPEAKERS
                for label, (start, end) in list(refs.items())[:MAX_KNOWN_SPEAKERS]:
                    number = numbers.number((0, label))
                    name = f"spk{number}"
                    name_to_number[name] = number
                    known.append((name, speaker_reference(path, start, end, work_dir, name)))
            for seg in segs:
                label = seg.get("speaker")
                if i > 0 and label in name_to_number:
                    number = name_to_number[label]
                else:
                    number = numbers.number((i, label))  # 1-bo'lakda bo'lmagan yangi spiker
                result.append({"start": float(seg["start"]) + offset, "end": float(seg["end"]) + offset,
                               "speaker": number})
            if log:
                log(f"Spikerlar: {i + 1}/{len(chunks)} bo'lak ajratildi.")
    return result, seconds, too_many
