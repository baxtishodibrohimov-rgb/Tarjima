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
import tts_plan
from storage import TTS_DIR, MAX_ACTIVE_TTS_JOBS, safe_name
from timing_contract import TTS_CPS_ESTIMATE, TTS_MAX_CHARS

AISHA_API_BASE = os.environ.get("AISHA_API_BASE", "https://back.aisha.group").rstrip("/")
CACHE_DIR = TTS_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TTS_QUEUE: asyncio.Queue = asyncio.Queue()
PAUSE_FLAGS: dict = {}
CANCEL_FLAGS: dict = {}

# Faqat oldindan baho uchun (timing_contract.TTS_CPS_ESTIMATE bilan bir xil).
UZBEK_CHARS_PER_SECOND = TTS_CPS_ESTIMATE

# Aisha TTS narxi - har bir belgi (harf) uchun 1 so'm. Dollarga
# AYLANTIRILMAYDI (kurs vaqt o'tishi bilan eskirib, noto'g'ri ko'rsatishi
# mumkin edi) - to'g'ridan-to'g'ri so'mda hisoblanib, alohida ustunda
# (costs.amount_som) saqlanadi. Narx environment variable orqali sozlanishi
# mumkin.
AISHA_SOM_PER_CHAR = float(os.environ.get("AISHA_SOM_PER_CHAR", "1.0"))


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


# TTS har doim 1.0 tezlikda so'raladi (kesh barqaror). Tezlik (tempo) keyin,
# yig'ishda, lektor sur'ati bo'yicha pitch saqlanadigan usulda qo'llanadi
# (tts_plan.compute_tempos + _apply_tempo). Eski [speed:fast]/[speed:slow]
# teglari o'qiladi, lekin e'tiborsiz qoldiriladi.
TTS_SPEED = 1.0


# ---------------------------------------------------------------------------
#                          PROVIDERLAR
# ---------------------------------------------------------------------------

async def aisha_generate_one(client: httpx.AsyncClient, text: str, mood: str, speed: float, api_key: str) -> bytes:
    if not AISHA_API_BASE:
        raise RuntimeError("AISHA_API_BASE sozlanmagan (environment variable orqali kiriting).")
    data = {"language": "uz", "model": "Gulnoza", "mood": mood, "speed": str(speed),
             "transcript": text[:TTS_MAX_CHARS["aisha"]]}
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
    body = {"model": "gpt-4o-mini-tts", "voice": voice, "input": text[:TTS_MAX_CHARS["openai"]], "response_format": "wav",
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
               stretch_to_fit: bool = True, video_id: str = None, for_track: bool = False,
               owner_id: str = None, voice_map: dict = None) -> str:
    """TTS ishini yaratadi. `segments` - yakuniy SRT bloklari; TTS birligi esa
    gap (tts_plan.build_units). `speed` eski API bilan moslik uchun qabul
    qilinadi, lekin ishlatilmaydi - TTS har doim 1.0 tezlikda so'raladi."""
    job_id = db.new_id()
    if not owner_id and video_id:
        video = db.fetchone("SELECT owner_id FROM videos WHERE id = ?", (video_id,))
        owner_id = video["owner_id"] if video else None
    aisha_enc = keys_manager.encrypt_raw(aisha_key) if aisha_key else None
    units = tts_plan.build_units(segments, provider)
    db.execute(
        """INSERT INTO tts_jobs (id, title, provider, voice, mood, speed, instructions,
           aisha_key_encrypted, stretch_to_fit, status, total_segments, completed_segments, created_at, video_id,
           for_track, owner_id, voice_map)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?, ?)""",
        (job_id, title or "TTS ishi", provider, voice, mood, TTS_SPEED, instructions,
         aisha_enc, 1 if stretch_to_fit else 0, len(units), db.now(), video_id, 1 if for_track else 0, owner_id,
         json.dumps(voice_map, ensure_ascii=False) if voice_map else None),
    )
    # Matni bo'sh blok - foydalanuvchi ataylab "tarjima qilmayman, o'tkazib
    # yubor" desa shu yerga tushadi: TTS'ga umuman yuborilmaydi, yakuniy
    # audioda shu joyda jim (silence) qoladi.
    _insert_units(job_id, units, lambda u: "skipped" if u["skipped"] else "pending")
    skipped_count = sum(1 for u in units if u["skipped"])
    if skipped_count:
        db.execute("UPDATE tts_jobs SET completed_segments = ? WHERE id = ?", (skipped_count, job_id))
    TTS_QUEUE.put_nowait(job_id)
    return job_id


def _insert_units(job_id: str, units: list, status_for, reuse: dict = None):
    for i, u in enumerate(units):
        old = (reuse or {}).get(_unit_identity(u))
        if old:
            status, audio_path, cache_key = "completed", old["audio_path"], old["cache_key"]
        else:
            status, audio_path, cache_key = status_for(u), None, None
        db.execute(
            """INSERT INTO tts_segments (id, job_id, seg_index, start_sec, end_sec, text, status, audio_path,
               cache_key, sentence_index, block_start, block_end, part_index, speaker)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (db.new_id(), job_id, i, u["start"], u["end"], u["text"], status, audio_path, cache_key,
             u["sentence_index"], u["block_start"], u["block_end"], u["part_index"], u.get("speaker")),
        )


def _unit_identity(u: dict) -> tuple:
    return ((u.get("text") or "").strip(), u.get("speaker"))


def is_legacy_job(job_id: str) -> bool:
    """Eski ish: har SRT bloki alohida TTS qilingan (sentence_index yo'q).
    Bunday ishlarda eski audio fayllar qayta ishlatiladi - yig'ishda bloklar
    gaplarga birlashtiriladi, tahrirda faqat o'zgargan blok qayta yaratiladi."""
    row = db.fetchone("SELECT COUNT(*) c FROM tts_segments WHERE job_id = ? AND sentence_index IS NOT NULL",
                      (job_id,))
    return not row or row["c"] == 0


def unit_blocks(seg: dict) -> range:
    """Birlik qamragan yakuniy SRT bloklari (eski ishlarda seg_index = blok)."""
    if seg.get("block_start") is None:
        return range(seg["seg_index"], seg["seg_index"] + 1)
    return range(seg["block_start"], (seg["block_end"] if seg.get("block_end") is not None else seg["block_start"]) + 1)


def block_audio_status(job_id: str) -> dict:
    """{blok indeksi: {"status", "error", "duration_overflow"}} - blok kirgan
    gap(lar)ning umumiy holati ('Tahrirlash va audio' uchun)."""
    result = {}
    rank = {"error": 4, "running": 3, "pending": 2, "completed": 1, "skipped": 0}
    for s in db.fetchall("SELECT * FROM tts_segments WHERE job_id = ?", (job_id,)):
        for i in unit_blocks(s):
            cur = result.get(i)
            if cur is None or rank.get(s["status"], 2) > rank.get(cur["status"], 2):
                result[i] = {"status": s["status"], "error": s["error"],
                             "duration_overflow": bool(s["duration_overflow"])}
            elif s["duration_overflow"]:
                cur["duration_overflow"] = True
    return result


def rebuild_job_units(job_id: str, blocks: list, changed_blocks: list) -> int:
    """Bloklar tahrirlangandan keyin TTS birliklarini qayta quradi.

    Yangi ish: gaplar qaytadan guruhlanadi (tinish belgisi o'zgarsa chegaralar
    ham o'zgaradi); matni va spikeri o'zgarmagan gap tayyor audiosini saqlaydi,
    o'zgargan gaplar 'pending' bo'ladi (kesh bor bo'lsa pul sarflanmaydi).
    Eski ish: faqat o'zgargan bloklar qayta yaratiladi, qolgan eski audio saqlanadi.
    Qaytaradi: qayta yaratiladigan gaplar (eski ishda bloklar) soni."""
    job = db.fetchone("SELECT provider FROM tts_jobs WHERE id = ?", (job_id,))
    if not job:
        return 0
    if is_legacy_job(job_id):
        for i in changed_blocks:
            text = (blocks[i].get("text") or "") if 0 <= i < len(blocks) else ""
            db.execute("UPDATE tts_segments SET text = ?, status = 'pending', audio_path = NULL, "
                       "cache_key = NULL, error = NULL, duration_overflow = 0 WHERE job_id = ? AND seg_index = ?",
                       (text, job_id, i))
        return len(changed_blocks)
    old = db.fetchall("SELECT * FROM tts_segments WHERE job_id = ?", (job_id,))
    reuse = {_unit_identity(o): o for o in old if o["status"] == "completed" and o["audio_path"]}
    skipped_before = {i for o in old if o["status"] == "skipped" for i in unit_blocks(o)}
    units = tts_plan.build_units(blocks, job["provider"])
    db.execute("DELETE FROM tts_segments WHERE job_id = ?", (job_id,))
    # Bo'sh blok faqat avval ham ataylab o'tkazib yuborilgan bo'lsa 'skipped';
    # yangi bo'shab qolgan blok 'pending' - yig'ishdan oldin to'ldirish so'raladi.
    _insert_units(job_id, units,
                  lambda u: "skipped" if u["skipped"] and u["block_start"] in skipped_before else "pending",
                  reuse)
    done = db.fetchone("SELECT COUNT(*) c FROM tts_segments WHERE job_id = ? AND status IN ('completed', 'skipped')",
                       (job_id,))["c"]
    _update_job(job_id, total_segments=len(units), completed_segments=done)
    pending = db.fetchall("SELECT DISTINCT sentence_index FROM tts_segments WHERE job_id = ? AND status = 'pending'",
                          (job_id,))
    return len(pending)


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

def voice_for_speaker(job: dict, speaker) -> tuple:
    """(voice, mood) - spikerga xaritada ovoz berilgan bo'lsa o'sha, aks holda asosiy."""
    voice, mood = job["voice"], job["mood"]
    try:
        voice_map = json.loads(job["voice_map"]) if job["voice_map"] else {}
    except (TypeError, ValueError):
        voice_map = {}
    entry = voice_map.get(str(speaker)) if speaker is not None else None
    if isinstance(entry, dict):
        voice = entry.get("voice") or voice
        mood = entry.get("mood") or mood
    return voice, mood


async def _process_segment(client, job, seg, lock, ctx, out_dir):
    if CANCEL_FLAGS.get(job["id"]) or PAUSE_FLAGS.get(job["id"]) or ctx["stop"]:
        return
    if not (seg["text"] or "").strip():
        db.execute("UPDATE tts_segments SET status = 'error', error = ? WHERE id = ?",
                   ("Tarjima matni bo'sh - 'Tahrirlash va audio' bo'limida shu bo'lak uchun matn kiriting.",
                    seg["id"]))
        return
    provider = job["provider"]
    voice, mood = voice_for_speaker(job, seg["speaker"] if "speaker" in seg.keys() else None)

    if provider == "aisha":
        raw = keys_manager.decrypt_raw(job["aisha_key_encrypted"]) if job["aisha_key_encrypted"] else ""
        if not raw:
            async with lock:
                _update_job(job["id"], status="error", error="Aisha API kalit topilmadi.")
                ctx["stop"] = True
            _notify_video(job["id"])
            return
        key_params = {"mood": mood, "speed": TTS_SPEED}
        ext = "wav"
    else:
        kid, raw = keys_manager.get_next_active_key(owner_id=job["owner_id"])
        if not raw:
            async with lock:
                db.execute("UPDATE tts_segments SET status = 'pending' WHERE id = ?", (seg["id"],))
                _update_job(job["id"], status="paused_api_key",
                            error="Ishlaydigan OpenAI API kalit topilmadi. Yangi API kalit kiriting.")
                ctx["stop"] = True
            _notify_video(job["id"])
            return
        key_params = {"voice": voice, "instructions": job["instructions"], "speed": TTS_SPEED}
        ext = "wav"

    key = cache_key_for(provider, seg["text"], **key_params)
    cpath = cache_path(key, ext)

    db.execute("UPDATE tts_segments SET status = 'running' WHERE id = ?", (seg["id"],))
    try:
        if cpath.exists():
            audio_bytes = cpath.read_bytes()
            from_cache = True
        else:
            # Vaqtinchalik xatolar (tarmoq uzilishi, "rate limit" va h.k.) uchun
            # bir necha marta qayta urinamiz - aks holda 1000+ segmentli katta
            # ishlarda BITTA vaqtinchalik muvaffaqiyatsizlik butun ishni
            # to'xtatib qo'yardi (foydalanuvchi qo'lda "Davom ettirish"ni
            # bosishiga to'g'ri kelardi).
            last_err = None
            for attempt in range(3):
                try:
                    if provider == "aisha":
                        audio_bytes = await aisha_generate_one(client, seg["text"], mood, TTS_SPEED, raw)
                    else:
                        audio_bytes = await openai_tts_generate_one(
                            client, seg["text"], voice, raw, job["instructions"], speed=TTS_SPEED)
                    last_err = None
                    break
                except Exception as retry_err:
                    last_err = retry_err
                    if attempt < 2:
                        await asyncio.sleep(2 * (attempt + 1))
            if last_err:
                raise last_err
            cpath.write_bytes(audio_bytes)
            from_cache = False
        # Fayl nomi birlik id'si bilan - tahrirdan keyin birliklar qayta raqamlansa
        # ham saqlab qolingan boshqa birlikning faylini bosib ketmaydi.
        seg_path = out_dir / f"seg_{seg['id']}.{ext}"
        seg_path.write_bytes(audio_bytes)

        async with lock:
            db.execute("UPDATE tts_segments SET status = 'completed', audio_path = ?, cache_key = ?, "
                       "applied_speed = ?, duration_overflow = 0 WHERE id = ?",
                       (str(seg_path), key, TTS_SPEED, seg["id"]))
            done = db.fetchone(
                "SELECT COUNT(*) c FROM tts_segments WHERE job_id = ? AND status IN ('completed', 'skipped')",
                (job["id"],))["c"]
            _update_job(job["id"], completed_segments=done)
            db.log_line(job["id"], f"Segment {seg['seg_index']+1} tayyor{' (kesh)' if from_cache else ''}.")
            if not from_cache:
                if provider == "aisha":
                    chars = len(seg["text"][:TTS_MAX_CHARS["aisha"]])
                    som_cost = round(chars * AISHA_SOM_PER_CHAR, 2)
                    db.add_cost(job["video_id"], "tts_aisha", amount_usd=0, amount_som=som_cost,
                                 detail=f"Aisha TTS, segment {seg['seg_index']+1}, ~{chars} belgi",
                                 owner_id=job["owner_id"])
                else:
                    chars = len(seg["text"][:TTS_MAX_CHARS["openai"]])
                    cost = round((chars / 1000) * 0.015, 6)
                    db.add_cost(job["video_id"], "tts_openai", cost,
                                 detail=f"OpenAI TTS, segment {seg['seg_index']+1}, ~{chars} belgi",
                                 owner_id=job["owner_id"])
        if provider != "aisha":
            keys_manager.mark_result(kid, True)
    except Exception as e:
        async with lock:
            db.execute("UPDATE tts_segments SET status = 'error', error = ?, attempts = attempts + 1 WHERE id = ?",
                       (str(e)[:500], seg["id"]))
            db.log_line(job["id"], f"XATO (segment {seg['seg_index']+1}): {e}")
        # MUHIM: kalit muvaffaqiyatsizligini QAYD ETAMIZ - aks holda
        # get_next_active_key() bu kalitni "muammoli" deb bilmaydi va
        # keyingi segmentlar ham xuddi shu (masalan "rate limit"ga uchragan)
        # kalitga yuborilishda davom etadi, xatolar zanjirini kuchaytiradi.
        if provider != "aisha":
            keys_manager.mark_result(kid, False, str(e)[:500])


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

    # Merge/render BOSHLANISHIDAN OLDIN: agar bog'langan videoning biror YAKUNIY
    # tarjima blokida HOZIR matn bo'sh bo'lsa-yu, bu segment ataylab "o'tkazib
    # yuborish" (skipped, foydalanuvchi oldindan tasdiqlagan) sifatida
    # belgilanmagan bo'lsa (masalan, qayta-transkripsiya orqali tarjima
    # tozalangan-u, hali qayta tarjima qilinmagan) - jim bo'shliq bilan sirtli
    # davom etish o'rniga, aniq xabar bilan TO'XTATILADI.
    if job and job["video_id"] and not job["for_track"]:
        v = db.fetchone("SELECT translation_segments FROM videos WHERE id = ?", (job["video_id"],))
        if v:
            blocks = json.loads(v["translation_segments"] or "[]")
            status_by_block = {}
            for seg in segs:
                for i in unit_blocks(seg):
                    status_by_block.setdefault(i, set()).add(seg["status"])
            missing_blocks = []
            for i, block in enumerate(blocks):
                if (block.get("text") or "").strip():
                    continue
                if status_by_block.get(i) != {"skipped"}:
                    missing_blocks.append(i + 1)
            if missing_blocks:
                shown = ", ".join(str(x) for x in missing_blocks[:20])
                more = " ..." if len(missing_blocks) > 20 else ""
                msg = (f"{len(missing_blocks)} ta blokning tarjimasi bo'sh ({shown}{more}) - audio/video "
                       "yig'ishdan oldin ularni to'ldiring yoki ataylab o'tkazib yuborishni tasdiqlang.")
                _update_job(job_id, status="error", error=msg)
                db.log_line(job_id, f"XATO (merge oldindan tekshiruv): {msg}")
                _notify_video(job_id)
                return

    ok_segs = [s for s in segs if s["status"] == "completed" and s["audio_path"]]
    if not ok_segs:
        _update_job(job_id, status="error", error="Birlashtirish uchun tayyor segment yo'q.")
        _notify_video(job_id)
        return

    video_duration = 0.0
    pace_items = []
    if job["video_id"]:
        v = db.fetchone("SELECT duration, transcript_words, transcript_segments FROM videos WHERE id = ?",
                        (job["video_id"],))
        if v and v["duration"]:
            video_duration = float(v["duration"])
        if v:
            # Lektor sur'ati: so'z vaqtlari bo'lsa - ulardan, bo'lmasa original SRT bloklaridan.
            try:
                pace_items = tts_plan.pace_items_from_words(json.loads(v["transcript_words"] or "[]"))
                if not pace_items:
                    pace_items = tts_plan.pace_items_from_segments(json.loads(v["transcript_segments"] or "[]"))
            except (TypeError, ValueError):
                pace_items = []

    loop = asyncio.get_event_loop()
    out_path = TTS_DIR / job_id / f"{safe_name(job['title'])}_yakuniy.mp3"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        points, stats = await loop.run_in_executor(
            None, merge_sentences, segs, out_path, bool(job["stretch_to_fit"]),
            video_duration, job_id, job["video_id"], pace_items)
        _update_job(job_id, status="completed", finished_at=db.now(), result_path=str(out_path), error=None,
                    freeze_points=json.dumps(points, ensure_ascii=False),
                    tempo_stats=json.dumps(stats, ensure_ascii=False))
        timeline_text = transcription.timeline_message(points)
        db.log_line(job_id, f"Yakuniy audio yig'ildi: {stats['sentences']} ta gap, ovoz tezligi "
                             f"{stats['tempo_min']:.2f}-{stats['tempo_max']:.2f} (o'rtacha {stats['tempo_avg']:.2f}, "
                             f"asosiy {stats['base']:.2f}, {stats['engine']})."
                             f"{' Videoda: ' + timeline_text + '.' if timeline_text else ''}")
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
    """Chiziqli interpolatsiya bilan uzunlikni o'zgartiradi (pitch ham o'zgaradi!) -
    endi faqat format moslash uchun; tempo _apply_tempo (atempo/rubberband) bilan."""
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


# Gap bo'laklari (uzun gap provayder chegarasida bo'linganda) orasidagi pauza.
PART_GAP_SEC = 0.12
# Ovoz boshida/oxirida qoldiriladigan jimlik (TTS bergan uzun jimlik kesiladi).
LEAD_PAD_SEC = 0.03
TAIL_PAD_SEC = 0.06
SILENCE_THRESHOLD = 300  # 16-bit amplituda (~ -40 dBFS)

_TEMPO_ENGINE = None


def tempo_engine() -> str:
    """Pitch saqlanadigan tempo filtri: rubberband (mavjud bo'lsa) yoki atempo."""
    global _TEMPO_ENGINE
    if _TEMPO_ENGINE is None:
        try:
            out = subprocess.run([transcription.ffmpeg_exe(), "-hide_banner", "-filters"], capture_output=True,
                                 text=True, errors="ignore", timeout=30).stdout
        except Exception:
            out = ""
        _TEMPO_ENGINE = "rubberband" if " rubberband " in out else "atempo"
    return _TEMPO_ENGINE


def _tempo_filter(tempo: float) -> str:
    if tempo_engine() == "rubberband":
        return f"rubberband=tempo={tempo:.4f}"
    return f"atempo={tempo:.4f}"


def _apply_tempo(pcm: bytes, nchannels: int, framerate: int, tempo: float) -> bytes:
    """16-bit PCM ovoz tezligini pitch'ni o'zgartirmasdan o'zgartiradi."""
    if abs(tempo - 1.0) < 0.002 or not pcm:
        return pcm
    fmt = ["-f", "s16le", "-ar", str(framerate), "-ac", str(nchannels)]
    proc = subprocess.run([transcription.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", *fmt, "-i", "pipe:0",
                           "-af", _tempo_filter(tempo), *fmt, "pipe:1"],
                          input=pcm, capture_output=True, timeout=300)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"Ovoz tezligini o'zgartirib bo'lmadi (ffmpeg): "
                           f"{proc.stderr.decode('utf-8', 'ignore')[-500:]}")
    return proc.stdout[:len(proc.stdout) - len(proc.stdout) % (2 * nchannels)]


def _to_pcm16(path: Path, nchannels: int = None, framerate: int = None):
    """WAV -> (16-bit PCM, kanallar, sample rate). Format boshqacha bo'lsa
    (kanal/sample rate) - ffmpeg bilan umumiy formatga keltiriladi."""
    ch, width, rate, raw = _read_wav_file(path)
    if width == 2 and (nchannels is None or (ch == nchannels and rate == framerate)):
        return raw, ch, rate
    nchannels = nchannels or ch
    framerate = framerate or rate
    proc = subprocess.run([transcription.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                           "-f", "s16le", "-ar", str(framerate), "-ac", str(nchannels), "pipe:1"],
                          capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"Audio formatini o'zgartirib bo'lmadi: {path.name}")
    return proc.stdout, nchannels, framerate


def _trim_silence(pcm: bytes, nchannels: int, framerate: int) -> bytes:
    """TTS bergan boshidagi/oxiridagi uzun jimlikni kesadi (ozgina zaxira qoladi) -
    shunda gap ovozi aynan gap boshida eshitiladi va keraksiz sekinlashish bo'lmaydi."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    n = len(samples)
    win = max(int(framerate * 0.01), 1) * nchannels

    def loud(i):
        return max((abs(x) for x in samples[i:i + win]), default=0) > SILENCE_THRESHOLD

    first = 0
    while first < n and not loud(first):
        first += win
    if first >= n:
        return b""
    last = n - (n % win or win)
    while last > first and not loud(last):
        last -= win
    start = max(first - int(LEAD_PAD_SEC * framerate) * nchannels, 0)
    end = min(last + win + int(TAIL_PAD_SEC * framerate) * nchannels, n)
    start -= start % nchannels
    end -= end % nchannels
    return samples[start:end].tobytes()


def _sentence_pcm(sentence: dict, fmt: dict) -> bytes:
    """Gap birliklari (bo'laklari) ovozini ketma-ket qo'shadi va jimlikni kesadi."""
    gap = bytes(int(PART_GAP_SEC * fmt["rate"]) * fmt["channels"] * 2)
    pieces = []
    for u in sentence["units"]:
        if u["status"] != "completed" or not u.get("audio_path"):
            continue
        pcm, _, _ = _to_pcm16(Path(u["audio_path"]), fmt["channels"], fmt["rate"])
        pcm = _trim_silence(pcm, fmt["channels"], fmt["rate"])
        if pcm:
            pieces.append(pcm)
    return gap.join(pieces)


def merge_sentences(segs: list, out_path: Path, stretch_to_fit: bool, video_duration: float = 0.0,
                    job_id: str = None, video_id: str = None, pace_items: list = None):
    """Yakuniy audio: har gap ovozi videoda shu gapning original boshlanish
    joyida boshlanadi (MASTER INSTRUKSIYA).

    1) Birliklar gaplarga yig'iladi (eski ishlarda bloklar audiosi qayta ishlatiladi).
    2) D_i - gap ovozining 1.0 tezlikdagi davomiyligi, A_i - keyingi gapgacha vaqt.
    3) tempo_i - lektor sur'atiga ergashadi (tts_plan.compute_tempos), pitch saqlanadi.
    4) need_i = D_i / tempo_i > A_i bo'lsa video sekinlashadi (slow), juda kam holatda kutadi.
    5) Har gap ovozi source_time_to_final_time(gap boshi) da yoziladi - ustma-ust tushmaydi.
    Qaytaradi: (vaqt nuqtalari, statistika)."""
    sentences = [s for s in tts_plan.group_sentences(segs) if s["voiced"]]
    if not sentences:
        raise RuntimeError("Birlashtirish uchun tayyor ovoz yo'q.")
    for s in sentences:
        s["start"] = round(s["start"], 3)
        s["end"] = max((u["end_sec"] or s["start"]) for u in s["units"])
    first = next(u for s in sentences for u in s["units"] if u["status"] == "completed" and u.get("audio_path"))
    ch0, _, rate0, _ = _read_wav_file(Path(first["audio_path"]))
    fmt = {"channels": ch0, "rate": rate0}
    bytes_per_sec = fmt["rate"] * fmt["channels"] * 2

    # 1-o'tish: D_i (xotirani tejash uchun ovozlar saqlanmaydi - 2-o'tishda qayta o'qiladi).
    durations = [len(_sentence_pcm(s, fmt)) / bytes_per_sec for s in sentences]
    starts = [s["start"] for s in sentences]
    available = tts_plan.available_times(starts, video_duration)
    paces = tts_plan.sentence_paces(sentences, pace_items or [])
    speakers = [s.get("speaker") for s in sentences]
    tempos, base = tts_plan.compute_tempos(durations, available, paces, speakers)

    # 2-o'tish: tempo qo'llanadi (parallel ffmpeg), haqiqiy uzunlik bilan reja tuziladi.
    from concurrent.futures import ThreadPoolExecutor

    def render(i):
        return _apply_tempo(_sentence_pcm(sentences[i], fmt), fmt["channels"], fmt["rate"], tempos[i])

    tmp_dir = out_path.parent / "sentences"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    needs = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        # Kichik guruhlar bilan - uzun videoda barcha ovozlar bir vaqtda xotirada turmasin.
        for batch_start in range(0, len(sentences), 16):
            batch = range(batch_start, min(batch_start + 16, len(sentences)))
            for i, pcm in zip(batch, pool.map(render, batch)):
                (tmp_dir / f"{i:05d}.pcm").write_bytes(pcm)
                needs.append(len(pcm) / bytes_per_sec)

    points = tts_plan.plan_points(starts, available, needs) if stretch_to_fit else []
    active = transcription.active_timeline_points(points)

    merged_wav_path = out_path.with_suffix(".merged.wav")
    total_end = transcription.source_time_to_final_time(video_duration, active) if video_duration > 0 else 0.0
    frame_bytes = fmt["channels"] * 2
    written = 0
    max_start_error = 0.0
    try:
        with wave.open(str(merged_wav_path), "wb") as wf:
            wf.setnchannels(fmt["channels"])
            wf.setsampwidth(2)
            wf.setframerate(fmt["rate"])
            for i, s in enumerate(sentences):
                pcm = (tmp_dir / f"{i:05d}.pcm").read_bytes()
                final_start = transcription.source_time_to_final_time(s["start"], active)
                start_frame = int(round(final_start * fmt["rate"]))
                if start_frame > written:
                    wf.writeframes(bytes((start_frame - written) * frame_bytes))
                    written = start_frame
                elif start_frame < written:
                    # Ustma-ust tushish (faqat stretch_to_fit o'chiq yoki yaxlitlashda) -
                    # yangi gap boshidagi zaxira jimlik qisqaradi.
                    pcm = pcm[(written - start_frame) * frame_bytes:]
                max_start_error = max(max_start_error, abs(written / fmt["rate"] - final_start))
                wf.writeframes(pcm)
                written += len(pcm) // frame_bytes
                total_end = max(total_end, written / fmt["rate"])
            end_frame = int((total_end + 0.3) * fmt["rate"])
            if end_frame > written:
                wf.writeframes(bytes((end_frame - written) * frame_bytes))
    finally:
        for f in tmp_dir.glob("*.pcm"):
            f.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass

    if job_id:
        overflow_sentences = []
        for p in active:
            if p["type"] != "freeze":
                continue
            idx = max((i for i, st in enumerate(starts) if st < p["time"]), default=0)
            overflow_sentences.append(idx)
            unit = sentences[idx]["units"][0]
            db.log_line(job_id, f"KUTISH (freeze): {idx + 1}-gap ({starts[idx]:.2f}s) ovozi sekinlashtirilgan "
                                f"videoga ham sig'madi - {p['time']:.2f}s da {p['duration']:.2f}s kutiladi.")
            db.execute("INSERT INTO freeze_point_events (id, video_id, tts_job_id, seg_index, source_time, "
                       "duration, applied_speed, created_at, type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'freeze')",
                       (db.new_id(), video_id, job_id, unit["seg_index"], p["time"], p["duration"],
                        tempos[idx], db.now()))
        for idx in overflow_sentences:
            for u in sentences[idx]["units"]:
                db.execute("UPDATE tts_segments SET duration_overflow = 1 WHERE id = ?", (u["id"],))
        for s, t, d in zip(sentences, tempos, durations):
            for u in s["units"]:
                db.execute("UPDATE tts_segments SET tempo = ?, audio_duration = ? WHERE id = ?",
                           (t, round(d, 3), u["id"]))

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

    summary = transcription.timeline_summary(active)
    stats = {
        "sentences": len(sentences),
        "tempo_min": min(tempos), "tempo_max": max(tempos),
        "tempo_avg": round(sum(tempos) / len(tempos), 3), "base": base,
        "engine": tempo_engine(), "max_start_error": round(max_start_error, 3),
        **summary,
    }
    return active, stats


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
