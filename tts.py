"""
Matn -> Audio: Aisha AI va OpenAI TTS providerlari, segment-darajasida
persistent job, natija keshlash va pure-Python audio yig'ish.

Yakuniy audio endi ffmpeg'ning murakkab filtr grafigi (amix) o'rniga
to'g'ridan-to'g'ri Python orqali (wave/array standart kutubxonalari bilan)
yig'iladi - bu tezroq, versiyaga bog'liq bo'lmagan va ishonchliroq. ffmpeg
faqat oxirida bitta oddiy (filtrsiz) MP3'ga siqish uchun ishlatiladi.
"""
import array
import asyncio
import hashlib
import io
import json
import os
import subprocess
import traceback
import wave
from pathlib import Path

import httpx

import database as db
import keys_manager
import transcription
from storage import TTS_DIR, MAX_ACTIVE_TTS_JOBS, safe_name

AISHA_API_BASE = os.environ.get("AISHA_API_BASE", "https://back.aisha.group").rstrip("/")
CACHE_DIR = TTS_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TTS_QUEUE: asyncio.Queue = asyncio.Queue()
PAUSE_FLAGS: dict = {}
CANCEL_FLAGS: dict = {}

# TTS'ga yuborishdan OLDIN, matn uzunligiga qarab tezlikni moslashtirish uchun
# (audio yaratilgach ffmpeg bilan siqishdan ko'ra tabiiyroq eshitiladi). Bundan
# ortiq tezlashtirish endi UMUMAN qilinmaydi - o'rniga video "kutib turadi"
# (freeze-frame, transcription.mux_video_audio_with_freezes orqali).
UZBEK_CHARS_PER_SECOND = 14.0
MAX_TTS_SPEED = float(os.environ.get("MAX_AUDIO_SPEEDUP", "1.15"))


def _update_job(job_id: str, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE tts_jobs SET {sets} WHERE id = ?", list(fields.values()) + [job_id])


def cache_key_for(provider: str, text: str, **params) -> str:
    raw = provider + "|" + text + "|" + json.dumps(params, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_path(key: str, ext: str) -> Path:
    return CACHE_DIR / f"{key}.{ext}"


def estimate_speech_duration(text: str, chars_per_second: float = UZBEK_CHARS_PER_SECOND) -> float:
    """Matn uzunligi asosida, tabiiy tezlikda o'qilganda taxminan qancha vaqt ketishini baholaydi."""
    length = len(text.strip())
    if length == 0 or chars_per_second <= 0:
        return 0.0
    return length / chars_per_second


def compute_segment_speed(text: str, available_seconds: float, base_speed: float = 1.0,
                           max_speed: float = MAX_TTS_SPEED):
    """Segment matnini mavjud vaqt oralig'iga sig'dirish uchun TTS'ga yuboriladigan
    'speed' qiymatini oldindan hisoblaydi. Qaytaradi: (speed, ehtimol_sig'maydi: bool)."""
    base_speed = max(base_speed, 1.0)
    if not available_seconds or available_seconds <= 0:
        return base_speed, False
    needed = estimate_speech_duration(text)
    if needed <= available_seconds:
        return base_speed, False
    ratio = needed / available_seconds
    speed = min(max(base_speed, ratio), max_speed)
    return speed, (needed / speed) > available_seconds


# ---------------------------------------------------------------------------
#                          PROVIDERLAR
# ---------------------------------------------------------------------------

async def aisha_generate_one(client: httpx.AsyncClient, text: str, mood: str, speed: float, api_key: str) -> bytes:
    if not AISHA_API_BASE:
        raise RuntimeError("AISHA_API_BASE sozlanmagan (environment variable orqali kiriting).")
    data = {"language": "uz", "model": "Gulnoza", "mood": mood, "speed": str(speed),
             "transcript": text[:1000]}
    resp = await client.post(f"{AISHA_API_BASE}/api/v1/tts/post/",
                              headers={"X-Api-Key": api_key}, data=data)
    if resp.status_code >= 400:
        msg = f"HTTP {resp.status_code}"
        try:
            j = resp.json()
            if j.get("detail"):
                msg = j["detail"]
        except Exception:
            pass
        raise RuntimeError(f"Aisha xatosi: {msg}")
    result = resp.json()
    audio_path = result.get("audio_path")
    if not audio_path:
        raise RuntimeError("Aisha javobida audio_path topilmadi.")
    audio_url = audio_path if audio_path.startswith("http") else (AISHA_API_BASE + audio_path)
    audio_resp = await client.get(audio_url)
    if audio_resp.status_code >= 400:
        raise RuntimeError("Audio faylni yuklab bo'lmadi.")
    return audio_resp.content


async def openai_tts_generate_one(client: httpx.AsyncClient, text: str, voice: str,
                                   api_key: str, instructions: str, speed: float = 1.0) -> bytes:
    body = {"model": "gpt-4o-mini-tts", "voice": voice, "input": text[:2000], "response_format": "wav",
            "speed": min(max(speed, 0.25), 4.0)}
    if instructions:
        body["instructions"] = instructions[:1000]
    resp = await client.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
    )
    if resp.status_code >= 400:
        msg = f"HTTP {resp.status_code}"
        try:
            j = resp.json()
            if j.get("error"):
                msg = j["error"].get("message", msg)
        except Exception:
            pass
        raise RuntimeError(f"OpenAI xatosi: {msg}")
    return resp.content


# ---------------------------------------------------------------------------
#                          JOB YARATISH
# ---------------------------------------------------------------------------

def create_job(title: str, provider: str, segments: list, voice: str = "", mood: str = "",
               speed: float = 1.0, instructions: str = "", aisha_key: str = "",
               stretch_to_fit: bool = True, video_id: str = None) -> str:
    job_id = db.new_id()
    aisha_enc = keys_manager.encrypt_raw(aisha_key) if aisha_key else None
    db.execute(
        """INSERT INTO tts_jobs (id, title, provider, voice, mood, speed, instructions,
           aisha_key_encrypted, stretch_to_fit, status, total_segments, completed_segments, created_at, video_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?)""",
        (job_id, title or "TTS ishi", provider, voice, mood, speed, instructions,
         aisha_enc, 1 if stretch_to_fit else 0, len(segments), db.now(), video_id),
    )
    for i, seg in enumerate(segments):
        # Matni bo'sh bo'lak - foydalanuvchi ataylab "tarjima qilmayman, o'tkazib
        # yubor" desa shu yerga tushadi: TTS'ga umuman yuborilmaydi, yakuniy
        # audioda shu joyda jim (silence) qoladi (merge_job faqat 'completed'
        # bo'laklarni ishlatadi).
        status = "skipped" if not (seg["text"] or "").strip() else "pending"
        db.execute(
            """INSERT INTO tts_segments (id, job_id, seg_index, start_sec, end_sec, text, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (db.new_id(), job_id, i, seg["start"], seg["end"], seg["text"], status),
        )
    skipped_count = sum(1 for seg in segments if not (seg["text"] or "").strip())
    if skipped_count:
        db.execute("UPDATE tts_jobs SET completed_segments = ? WHERE id = ?", (skipped_count, job_id))
    TTS_QUEUE.put_nowait(job_id)
    return job_id


def resume_job(job_id: str):
    db.execute("UPDATE tts_segments SET status = 'pending' WHERE job_id = ? AND status = 'error'", (job_id,))
    _update_job(job_id, status="queued", error=None)
    PAUSE_FLAGS.pop(job_id, None)
    CANCEL_FLAGS.pop(job_id, None)
    TTS_QUEUE.put_nowait(job_id)


def retry_job(job_id: str):
    resume_job(job_id)


def pause_job(job_id: str):
    PAUSE_FLAGS[job_id] = True


def cancel_job(job_id: str):
    CANCEL_FLAGS[job_id] = True
    job = db.fetchone("SELECT status FROM tts_jobs WHERE id = ?", (job_id,))
    if job and job["status"] != "running":
        _update_job(job_id, status="cancelled")


def _notify_video(job_id: str):
    """Agar bu TTS ish biror video loyihasiga bog'langan bo'lsa, uni yangilaydi."""
    try:
        import worker
        worker.sync_video_from_tts_job(job_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
#                          SEGMENT ISHLASH
# ---------------------------------------------------------------------------

async def _process_segment(client, job, seg, lock, ctx, out_dir):
    if CANCEL_FLAGS.get(job["id"]) or PAUSE_FLAGS.get(job["id"]) or ctx["stop"]:
        return
    if not (seg["text"] or "").strip():
        db.execute("UPDATE tts_segments SET status = 'error', error = ? WHERE id = ?",
                   ("Tarjima matni bo'sh - 'Tahrirlash va audio' bo'limida shu bo'lak uchun matn kiriting.",
                    seg["id"]))
        return
    provider = job["provider"]

    # TTS'ga yuborishdan oldin, matn uzunligiga qarab tezlikni moslashtiraymiz
    # (o'z vaqt oynasiga tabiiy tarzda sig'ishi uchun, keyinroq sun'iy siqishdan ko'ra tabiiyroq eshitiladi)
    available = (seg["end_sec"] or 0) - (seg["start_sec"] or 0)
    base_speed = float(job["speed"] or 1.0) if provider == "aisha" else 1.0
    seg_speed, _likely_overflow = compute_segment_speed(seg["text"], available, base_speed=base_speed)

    if provider == "aisha":
        raw = keys_manager.decrypt_raw(job["aisha_key_encrypted"]) if job["aisha_key_encrypted"] else ""
        if not raw:
            async with lock:
                _update_job(job["id"], status="error", error="Aisha API kalit topilmadi.")
                ctx["stop"] = True
            _notify_video(job["id"])
            return
        aisha_speed = min(max(seg_speed, 0.5), 2.0)
        key_params = {"mood": job["mood"], "speed": aisha_speed}
        ext = "wav"
    else:
        kid, raw = keys_manager.get_next_active_key()
        if not raw:
            async with lock:
                db.execute("UPDATE tts_segments SET status = 'pending' WHERE id = ?", (seg["id"],))
                _update_job(job["id"], status="paused_api_key",
                            error="Ishlaydigan OpenAI API kalit topilmadi. Yangi API kalit kiriting.")
                ctx["stop"] = True
            _notify_video(job["id"])
            return
        key_params = {"voice": job["voice"], "instructions": job["instructions"], "speed": seg_speed}
        ext = "wav"

    key = cache_key_for(provider, seg["text"], **key_params)
    cpath = cache_path(key, ext)

    db.execute("UPDATE tts_segments SET status = 'running' WHERE id = ?", (seg["id"],))
    try:
        if cpath.exists():
            audio_bytes = cpath.read_bytes()
            from_cache = True
        else:
            if provider == "aisha":
                audio_bytes = await aisha_generate_one(client, seg["text"], job["mood"], aisha_speed, raw)
            else:
                audio_bytes = await openai_tts_generate_one(
                    client, seg["text"], job["voice"], raw, job["instructions"], speed=seg_speed)
            cpath.write_bytes(audio_bytes)
            from_cache = False
        seg_path = out_dir / f"seg_{seg['seg_index']:05d}.{ext}"
        seg_path.write_bytes(audio_bytes)
        async with lock:
            db.execute("UPDATE tts_segments SET status = 'completed', audio_path = ?, cache_key = ? WHERE id = ?",
                       (str(seg_path), key, seg["id"]))
            done = db.fetchone(
                "SELECT COUNT(*) c FROM tts_segments WHERE job_id = ? AND status IN ('completed', 'skipped')",
                (job["id"],))["c"]
            _update_job(job["id"], completed_segments=done)
            db.log_line(job["id"], f"Segment {seg['seg_index']+1} tayyor{' (kesh)' if from_cache else ''}.")
            if not from_cache:
                if provider == "aisha":
                    chars = len(seg["text"][:1000])
                    db.add_cost(job["video_id"], "tts", 0,
                                 detail=f"Aisha TTS, segment {seg['seg_index']+1}, ~{chars} belgi (~{chars} so'm)")
                else:
                    chars = len(seg["text"][:2000])
                    cost = round((chars / 1000) * 0.015, 6)
                    db.add_cost(job["video_id"], "tts", cost,
                                 detail=f"OpenAI TTS, segment {seg['seg_index']+1}, ~{chars} belgi")
        if provider != "aisha":
            keys_manager.mark_result(kid, True)
    except Exception as e:
        async with lock:
            db.execute("UPDATE tts_segments SET status = 'error', error = ?, attempts = attempts + 1 WHERE id = ?",
                       (str(e)[:500], seg["id"]))
            db.log_line(job["id"], f"XATO (segment {seg['seg_index']+1}): {e}")


async def run_tts_job(job_id: str):
    job = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    if not job or job["status"] == "cancelled":
        return
    _update_job(job_id, status="running", started_at=db.now())
    out_dir = TTS_DIR / job_id / "segments"
    out_dir.mkdir(parents=True, exist_ok=True)

    pending = db.fetchall("SELECT * FROM tts_segments WHERE job_id = ? AND status = 'pending' ORDER BY seg_index ASC",
                           (job_id,))
    if pending:
        sem = asyncio.Semaphore(4)
        lock = asyncio.Lock()
        ctx = {"stop": False}

        async def bound(seg):
            async with sem:
                await _process_segment(client, job, seg, lock, ctx, out_dir)

        async with httpx.AsyncClient(timeout=120) as client:
            await asyncio.gather(*(bound(s) for s in pending))

    job = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    if job["status"] in ("cancelled", "paused_api_key", "error"):
        PAUSE_FLAGS.pop(job_id, None)
        return
    if CANCEL_FLAGS.get(job_id):
        _update_job(job_id, status="cancelled")
        CANCEL_FLAGS.pop(job_id, None)
        _notify_video(job_id)
        return
    if PAUSE_FLAGS.get(job_id):
        _update_job(job_id, status="paused")
        PAUSE_FLAGS.pop(job_id, None)
        return

    remaining = db.fetchone(
        "SELECT COUNT(*) c FROM tts_segments WHERE job_id = ? AND status NOT IN ('completed', 'skipped')",
        (job_id,))["c"]
    if remaining == 0:
        await merge_job(job_id)
    else:
        err = db.fetchone("SELECT COUNT(*) c FROM tts_segments WHERE job_id = ? AND status = 'error'", (job_id,))["c"]
        _update_job(job_id, status="error", error=f"{err} ta segmentda xato.")
        _notify_video(job_id)


async def merge_job(job_id: str):
    job = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    segs = db.fetchall("SELECT * FROM tts_segments WHERE job_id = ? ORDER BY seg_index ASC", (job_id,))
    ok_segs = [s for s in segs if s["status"] == "completed" and s["audio_path"]]
    if not ok_segs:
        _update_job(job_id, status="error", error="Birlashtirish uchun tayyor segment yo'q.")
        _notify_video(job_id)
        return

    loop = asyncio.get_event_loop()
    out_path = TTS_DIR / job_id / f"{safe_name(job['title'])}_yakuniy.mp3"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        freeze_points = await loop.run_in_executor(
            None, _merge_segments_pure_python, ok_segs, out_path, bool(job["stretch_to_fit"]))
        _update_job(job_id, status="completed", finished_at=db.now(), result_path=str(out_path), error=None,
                    freeze_points=json.dumps(freeze_points, ensure_ascii=False))
        if freeze_points:
            db.log_line(job_id, f"Yakuniy audio yig'ildi. {len(freeze_points)} ta joyda video "
                                 f"'kutib turishi' kerak bo'ladi (audio uzunroq chiqdi).")
        else:
            db.log_line(job_id, "Yakuniy audio yig'ildi.")
    except Exception as e:
        _update_job(job_id, status="error", error=f"Birlashtirishda xato: {e}")
        db.log_line(job_id, f"XATO (merge): {e}\n{traceback.format_exc()[-400:]}")
    _notify_video(job_id)


# ---------------------------------------------------------------------------
#            PURE-PYTHON WAV O'QISH / RESAMPLE / BIRLASHTIRISH
# ---------------------------------------------------------------------------
# ffmpeg'ning murakkab filtr grafigi (amix, ko'p input) o'rniga: har bir
# bo'lak WAV sifatida to'g'ridan-to'g'ri Python massivida o'z vaqtiga
# "joylab qo'yiladi". Bu tezroq, versiyaga bog'liq emas va ishonchliroq.

_TYPECODE_BY_WIDTH = {1: "b", 2: "h", 4: "i"}


def _read_wav_file(path: Path):
    with wave.open(str(path), "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    return nchannels, sampwidth, framerate, raw


def _resample_raw(raw: bytes, nchannels: int, sampwidth: int, target_frame_count: int) -> bytes:
    """Oddiy chiziqli interpolatsiya orqali audio uzunligini target_frame_count'ga
    moslaydi (tezlik/pitch bir xilda o'zgaradi)."""
    typecode = _TYPECODE_BY_WIDTH.get(sampwidth)
    if typecode is None or target_frame_count <= 0:
        return raw
    samples = array.array(typecode)
    samples.frombytes(raw)
    total_samples = len(samples)
    orig_frame_count = total_samples // nchannels
    if orig_frame_count <= 1 or target_frame_count == orig_frame_count:
        return raw

    out = array.array(typecode, bytes(target_frame_count * nchannels * sampwidth))
    ratio = (orig_frame_count - 1) / max(target_frame_count - 1, 1)
    for i in range(target_frame_count):
        src_pos = i * ratio
        src_idx = int(src_pos)
        frac = src_pos - src_idx
        next_idx = min(src_idx + 1, orig_frame_count - 1)
        for ch in range(nchannels):
            a = samples[src_idx * nchannels + ch]
            b = samples[next_idx * nchannels + ch]
            out[i * nchannels + ch] = int(a + (b - a) * frac)
    return out.tobytes()


def _merge_segments_pure_python(ok_segs: list, out_path: Path, stretch_to_fit: bool, max_rate: float = 1.2):
    """Har bir bo'lak WAV faylini o'qib, bitta katta jim buferga joylaydi.

    Audio har doim TABIIY tezlikda o'qiladi. Agar bo'lak o'ziga ajratilgan
    vaqtga sig'masa:
      - Avval yengil tezlashtirish sinaladi (max_rate gacha, standart 1.2x -
        bu deyarli sezilmaydi);
      - Agar shundan keyin ham sig'masa, ortiqcha qism uchun "muzlatish
        nuqtasi" qaytariladi - buni video render bosqichi asl videoga
        qo'llab, o'sha joyda kadrni bir necha soniya "kutib turadi".

    Qaytaradi: freeze_points - [{"time": <original video vaqti>, "duration": <necha soniya kutish>}, ...]
    """
    parsed = []
    for s in ok_segs:
        nchannels, sampwidth, framerate, raw = _read_wav_file(Path(s["audio_path"]))
        parsed.append({**s, "nchannels": nchannels, "sampwidth": sampwidth, "framerate": framerate, "raw": raw})
    parsed.sort(key=lambda p: p["start_sec"])

    nchannels = parsed[0]["nchannels"]
    sampwidth = parsed[0]["sampwidth"]
    framerate = parsed[0]["framerate"]
    typecode = _TYPECODE_BY_WIDTH.get(sampwidth, "h")

    # 1-o'tish: har bir bo'lak uchun moslashtirilgan (surilgan) boshlanish vaqtini,
    # kerak bo'lsa yengil tezlashtirishni (<=max_rate) va "muzlatish nuqtalari"ni hisoblaymiz.
    cumulative_shift = 0.0
    freeze_points = []
    adjusted = []
    for idx, p in enumerate(parsed):
        raw = p["raw"]
        orig_frame_count = len(raw) // (p["nchannels"] * p["sampwidth"])
        orig_duration = orig_frame_count / p["framerate"] if p["framerate"] else 0

        adjusted_start = p["start_sec"] + cumulative_shift
        if idx + 1 < len(parsed):
            natural_gap = parsed[idx + 1]["start_sec"] - p["start_sec"]
        else:
            natural_gap = max(p["end_sec"] - p["start_sec"], orig_duration)

        effective_duration = orig_duration
        if stretch_to_fit and natural_gap > 0 and orig_duration > natural_gap:
            rate = min(orig_duration / natural_gap, max_rate)
            if rate > 1.001:
                target_frame_count = max(int(round(orig_frame_count / rate)), 1)
                raw = _resample_raw(raw, p["nchannels"], p["sampwidth"], target_frame_count)
                orig_frame_count = target_frame_count
                effective_duration = orig_frame_count / p["framerate"] if p["framerate"] else 0

            if effective_duration > natural_gap + 0.01:
                overflow = effective_duration - natural_gap
                freeze_points.append({
                    "time": round(p["start_sec"] + natural_gap, 3),
                    "duration": round(overflow, 3),
                })
                cumulative_shift += overflow

        adjusted.append({**p, "raw": raw, "adjusted_start": adjusted_start, "orig_duration": effective_duration})

    total_duration = adjusted[-1]["adjusted_start"] + adjusted[-1]["orig_duration"] + 3.0
    total_frames = int(total_duration * framerate)
    buffer = array.array(typecode, bytes(total_frames * nchannels * sampwidth))

    for p in adjusted:
        raw = p["raw"]
        if p["nchannels"] != nchannels or p["framerate"] != framerate:
            raw = _convert_format(raw, p["nchannels"], p["sampwidth"], p["framerate"], nchannels, framerate)

        seg_samples = array.array(typecode)
        seg_samples.frombytes(raw)
        orig_frame_count = len(seg_samples) // nchannels

        start_frame = int(p["adjusted_start"] * framerate)
        end_frame = min(start_frame + orig_frame_count, total_frames)
        n_to_copy = max(end_frame - start_frame, 0)
        for i in range(n_to_copy * nchannels):
            buffer[start_frame * nchannels + i] = seg_samples[i]

    merged_wav_path = out_path.with_suffix(".merged.wav")
    with wave.open(str(merged_wav_path), "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(buffer.tobytes())

    # Oxirida: bitta oddiy (filtrsiz) ffmpeg chaqiruvi - murakkab filtr grafigi yo'q,
    # shuning uchun ffmpeg versiyasiga bog'liq muammolar bo'lmaydi.
    cmd = [
        transcription.ffmpeg_exe(), "-y", "-i", str(merged_wav_path),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=transcription.FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        merged_wav_path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg audio birlashtirishda juda uzoq davom etdi va to'xtatildi. Qayta urinib ko'ring.")
    merged_wav_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout or "")[-2000:])
    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise RuntimeError(
            f"Yakuniy audio fayl yaratilmadi yoki bo'sh (hajm: {out_path.stat().st_size if out_path.exists() else 0} bayt)."
        )
    actual_duration = transcription.get_duration_seconds(out_path)
    if actual_duration < 1.0:
        raise RuntimeError(f"Yakuniy audio davomiyligi {actual_duration:.2f}s - bu noto'g'ri, fayl yaroqsiz bo'lishi mumkin.")
    return freeze_points


def _convert_format(raw: bytes, nchannels: int, sampwidth: int, framerate: int,
                     target_nchannels: int, target_framerate: int) -> bytes:
    """Kamdan-kam holatda providerlar boshqa sample-rate/kanal qaytarsa, umumiy formatga moslaydi."""
    import audioop
    if nchannels != target_nchannels:
        raw = audioop.tomono(raw, sampwidth, 0.5, 0.5) if target_nchannels == 1 else raw
    if framerate != target_framerate:
        raw, _ = audioop.ratecv(raw, sampwidth, target_nchannels, framerate, target_framerate, None)
    return raw


async def tts_consumer():
    while True:
        job_id = await TTS_QUEUE.get()
        try:
            await run_tts_job(job_id)
        except Exception as e:
            db.log_line(job_id, f"XATO (tts consumer): {e}\n{traceback.format_exc()[-400:]}")
            _update_job(job_id, status="error", error=str(e))
        finally:
            TTS_QUEUE.task_done()


async def recover_and_start():
    for j in db.fetchall("SELECT id FROM tts_jobs WHERE status IN ('running', 'queued')"):
        db.execute("UPDATE tts_segments SET status = 'pending' WHERE job_id = ? AND status = 'running'", (j["id"],))
        _update_job(j["id"], status="queued")
        TTS_QUEUE.put_nowait(j["id"])
    for _ in range(MAX_ACTIVE_TTS_JOBS):
        asyncio.create_task(tts_consumer())
