"""
Persistent background job tizimi: Video loyihasining butun hayot sikli.

Status vocabulary (videos.status): uploaded, segmenting, segments_ready,
transcribing, transcription_ready, transcription_approved, translation_ready,
audio_processing, audio_ready, video_rendering, completed, failed, cancelled.

Har bir "band" (blocked) holat videos.blocked_reason orqali ifodalanadi:
  None            - band emas, faol ishlamoqda yoki navbatda
  'paused'        - foydalanuvchi to'xtatgan
  'api_key'       - ishlaydigan OpenAI kalit yo'q
  'repetition'    - Whisper takrorlanish (hallucination) aniqlandi
  'chunk_errors'  - ba'zi bo'laklar xato bilan tugadi
  'error'         - umumiy xato (segmentlash/render bosqichida)

Bu ikki maydon (status + blocked_reason) UI'da aniq va sodda vaziyat
ko'rsatishga, shu bilan birga chunk-darajasidagi resume/retry mantig'ini
saqlab qolishga imkon beradi.
"""
import asyncio
import json
import shutil
import time
import traceback
from pathlib import Path

import httpx

import database as db
import keys_manager
import transcription
import translation
from storage import (CHUNKS_DIR, RESULTS_DIR, MAX_WHISPER_CONCURRENCY,
                      MAX_ACTIVE_VIDEO_JOBS, CHUNK_SECONDS, safe_name)

SEGMENT_QUEUE: asyncio.Queue = asyncio.Queue()
TRANSCRIBE_QUEUE: asyncio.Queue = asyncio.Queue()
RENDER_QUEUE: asyncio.Queue = asyncio.Queue()

PAUSE_FLAGS: dict = {}
CANCEL_FLAGS: dict = {}

# Hozir ishlayotgan (Whisper API'ga so'rov yuborilgan) bo'laklar - "sekinlashgan
# bo'lakni bekor qilib qayta urinish" funksiyasi uchun kerak.
RUNNING_CHUNK_TASKS: dict = {}   # chunk_id -> asyncio.Task
CHUNK_STARTED_AT: dict = {}      # chunk_id -> time.time() (unix timestamp)


def _update_video(video_id: str, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [video_id]
    db.execute(f"UPDATE videos SET {sets}, updated_at = ? WHERE id = ?",
               params[:-1] + [db.now(), video_id])


def log(video_id: str, msg: str):
    db.log_line(video_id, msg)


# ---------------------------------------------------------------------------
#                          LOYIHANI QAYTA BOSHLASH (RESTART)
# ---------------------------------------------------------------------------

# Bu holatlarda (va blocked_reason bo'lmasa - ya'ni haqiqatan FAOL ishlayotgan
# bo'lsa) restart xavfli: fon vazifasi hali ham eski ma'lumot ustida ishlab,
# tugagach restart bilan tozalangan holatni bosib qo'yishi (race condition)
# mumkin. Shuning uchun bunday paytda restart rad etiladi - foydalanuvchi
# avval "Bekor qilish"ni bosishi yoki tugashini kutishi kerak.
_RESTART_UNSAFE_ACTIVE_STATUSES = {"segmenting", "transcribing", "video_rendering"}


def restart_video(video_id: str):
    """Loyihani xuddi VIDEO YANGI YUKLANGANDAN keyingi holatga qaytaradi:
    transkripsiya, tarjima, audio va yakuniy video - bularning barchasi va
    ularga tegishli fayllar (bo'laklar, TTS audiosi, render natijasi)
    o'chiriladi, status 'uploaded'ga qaytariladi. Original video fayli va
    thumbnail SAQLANADI. Xarajatlar (costs) va umumiy ish jurnali (job_logs)
    ham SAQLANADI - bular haqiqiy sarflangan pul va tarixiy yozuv, ish
    natijalari emas.

    kind == 'pipeline' bo'lsa, video mavjud bo'lgani holda avtomatik
    segmentatsiya darhol qayta navbatga qo'yiladi (xuddi birinchi yuklashda
    bo'lgani kabi)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Video topilmadi.")
    if video["kind"] != "pipeline":
        raise ValueError("Faqat tarjima loyihalari uchun qayta boshlash mumkin.")

    if video["status"] in _RESTART_UNSAFE_ACTIVE_STATUSES and not video["blocked_reason"]:
        raise ValueError(
            f"Video hozir faol ishlamoqda ('{video['status']}') - qayta boshlashdan oldin "
            f"avval uni bekor qiling yoki tugashini kuting."
        )
    if video["status"] == "audio_processing" and video["tts_job_id"]:
        tts_job = db.fetchone("SELECT status FROM tts_jobs WHERE id = ?", (video["tts_job_id"],))
        if tts_job and tts_job["status"] in ("running", "queued"):
            raise ValueError(
                "Audio hozir faol yaratilmoqda - qayta boshlashdan oldin avval uni bekor qiling "
                "yoki tugashini kuting."
            )

    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)

    if video["tts_job_id"]:
        from storage import TTS_DIR
        import tts as tts_module
        tts_module.PAUSE_FLAGS.pop(video["tts_job_id"], None)
        tts_module.CANCEL_FLAGS.pop(video["tts_job_id"], None)
        db.execute("DELETE FROM tts_segments WHERE job_id = ?", (video["tts_job_id"],))
        db.execute("DELETE FROM tts_jobs WHERE id = ?", (video["tts_job_id"],))
        shutil.rmtree(TTS_DIR / video["tts_job_id"], ignore_errors=True)

    db.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
    db.execute("DELETE FROM results WHERE video_id = ?", (video_id,))
    shutil.rmtree(CHUNKS_DIR / video_id, ignore_errors=True)
    shutil.rmtree(RESULTS_DIR / video_id, ignore_errors=True)

    _update_video(
        video_id,
        status="uploaded", blocked_reason=None, progress=0,
        message="Loyiha boshidan boshlandi - bo'laklarga avtomatik qayta bo'linmoqda...",
        error=None, chunk_count=0, language="", instruction="", detected_language="",
        repetition_chunk_index=None, repetition_info=None,
        transcript_text=None, transcript_segments=None, transcript_approved=0,
        translation_text=None, translation_segments=None, translation_status="none", translation_source=None,
        audio_path=None, audio_status="none", tts_job_id=None,
        final_video_path=None, final_video_status="none", freeze_points=None,
        flagged_issues=None, topic_group=None,
        telegram_send_status="none", telegram_send_error=None, idea_flow_sent_at=None,
        split_total_parts=0, split_parts_sent=0,
    )
    log(video_id, "=== LOYIHA QAYTA BOSHLANDI (Restart): barcha ish natijalari (transkripsiya, "
                   "tarjima, audio, yakuniy video) tozalandi, video yuklangandan keyingi holatga "
                   "qaytarildi. Xarajatlar tarixi saqlanib qoldi. ===")

    if video["path"] and Path(video["path"]).exists():
        enqueue_segment(video_id)


# ---------------------------------------------------------------------------
#                          BO'LAKLARGA BO'LISH (SEGMENTATSIYA)
# ---------------------------------------------------------------------------

def enqueue_segment(video_id: str) -> bool:
    """Bo'laklarga bo'lishni navbatga qo'yadi. Video uchun bu ish ALLAQACHON
    faol ishlayotgan bo'lsa (status='segmenting' va xato/to'xtagan emas),
    qayta navbatga qo'ymaydi va False qaytaradi - bir faylni parallel ikki
    marta bo'lash yoki eski (allaqachon bo'langan) faylni bekorga qayta
    bo'lashning oldini olish uchun."""
    video = db.fetchone("SELECT status, blocked_reason FROM videos WHERE id = ?", (video_id,))
    if video and video["status"] == "segmenting" and not video["blocked_reason"]:
        return False
    _update_video(video_id, status="segmenting", blocked_reason=None,
                  message="Bo'laklarga bo'linmoqda...", error=None)
    log(video_id, "Bo'laklarga bo'lish navbatga qo'yildi.")
    SEGMENT_QUEUE.put_nowait(video_id)
    return True


async def segment_video(video_id: str):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    try:
        work_dir = CHUNKS_DIR / video_id
        shutil.rmtree(work_dir, ignore_errors=True)
        db.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))

        loop = asyncio.get_event_loop()
        chunks = await loop.run_in_executor(
            None, transcription.extract_and_chunk, Path(video["path"]), work_dir, CHUNK_SECONDS
        )
        for i, c in enumerate(chunks):
            db.execute(
                """INSERT INTO chunks (id, video_id, chunk_index, start_time, end_time, path, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (db.new_id(), video_id, i, c["start"], c["end"], str(c["path"]), db.now(), db.now()),
            )
        duration = chunks[-1]["end"] if chunks else (video["duration"] or 0)
        _update_video(video_id, status="segments_ready", blocked_reason=None, duration=duration,
                      chunk_count=len(chunks), message=f"Tayyor. {len(chunks)} ta bo'lak.", error=None)
        log(video_id, f"Bo'laklarga bo'lindi: {len(chunks)} ta bo'lak, {duration:.0f}s.")
    except Exception as e:
        _update_video(video_id, status="segmenting", blocked_reason="error", error=str(e),
                      message="Bo'laklarga bo'lishda xato.")
        log(video_id, f"XATO (segmentatsiya): {e}")


async def segment_consumer():
    while True:
        video_id = await SEGMENT_QUEUE.get()
        try:
            await segment_video(video_id)
        except Exception as e:
            log(video_id, f"XATO (segment consumer): {e}\n{traceback.format_exc()[-500:]}")
        finally:
            SEGMENT_QUEUE.task_done()


# ---------------------------------------------------------------------------
#                          TRANSKRIPSIYA
# ---------------------------------------------------------------------------

def start_transcription(video_id: str, language: str, instruction: str, topic_group: str = None):
    _update_video(video_id, status="transcribing", blocked_reason=None,
                  language=language or "", instruction=instruction or "", topic_group=topic_group or None,
                  progress=0, message="Navbatda...", error=None,
                  repetition_chunk_index=None, repetition_info=None)
    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)
    log(video_id, "Transkripsiya navbatga qo'yildi.")
    TRANSCRIBE_QUEUE.put_nowait(video_id)


def resume_job(video_id: str):
    db.execute("UPDATE chunks SET status = 'pending' WHERE video_id = ? AND status = 'error'", (video_id,))
    _update_video(video_id, status="transcribing", blocked_reason=None,
                  message="Navbatda (davom ettirilmoqda)...", error=None,
                  repetition_chunk_index=None, repetition_info=None)
    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)
    log(video_id, "Foydalanuvchi 'Davom ettirish'ni bosdi.")
    TRANSCRIBE_QUEUE.put_nowait(video_id)


def retry_chunk(video_id: str, chunk_id: str, language: str = None):
    """Bitta bo'lakni qayta transkripsiya qilish uchun navbatga qo'yadi. `language`
    berilsa (None emas - bo'sh satr ham "ataylab avtomatik" degani), shu bo'lak
    uchun til ATAYLAB shu qiymatga o'rnatiladi va keyingi barcha urinishlarda
    (ushbu retry ham, kelajakdagilar ham, qayta o'zgartirilmaguncha) ishlatiladi -
    shu bilan "qayta yuborish eski (noto'g'ri) til bilan yana xato natija beradi"
    muammosi tuzatiladi. force_split=1 bu bo'lakni kichik (30-60s) qismlarga
    bo'lib qayta ishlashni so'raydi (bir martalik, shu urinishdan keyin 0'ga tushadi)."""
    sets = ["status = 'pending'", "error = NULL", "force_split = 1", "updated_at = ?"]
    params = [db.now()]
    if language is not None:
        sets.append("language = ?")
        params.append(language)
    params += [chunk_id, video_id]
    db.execute(f"UPDATE chunks SET {', '.join(sets)} WHERE id = ? AND video_id = ?", params)
    _update_video(video_id, status="transcribing", blocked_reason=None,
                  message="Navbatda (bo'lak qayta ishlanmoqda)...")
    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)
    lang_note = f" (til: {storage_lang_label(language)})" if language is not None else ""
    log(video_id, f"Bo'lak {chunk_id} qayta ishlash uchun navbatga qo'yildi{lang_note}.")
    TRANSCRIBE_QUEUE.put_nowait(video_id)


def storage_lang_label(code: str) -> str:
    from storage import TRANSCRIBE_LANGUAGE_LABELS
    return TRANSCRIBE_LANGUAGE_LABELS.get(code or "", code or "avtomatik")


def retry_range(video_id: str, start: float, end: float):
    chunks = db.fetchall("SELECT id, chunk_index FROM chunks WHERE video_id = ? "
                          "AND NOT (end_time <= ? OR start_time >= ?)", (video_id, start, end))
    for c in chunks:
        db.execute("UPDATE chunks SET status = 'pending', error = NULL WHERE id = ?", (c["id"],))
    _update_video(video_id, status="transcribing", blocked_reason=None,
                  message=f"Vaqt oralig'i qayta ishlanmoqda ({len(chunks)} bo'lak)...")
    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)
    log(video_id, f"Vaqt oralig'i {transcription.fmt_minsec(start)}-{transcription.fmt_minsec(end)} "
                   f"qayta ishlash uchun navbatga qo'yildi ({len(chunks)} bo'lak).")
    TRANSCRIBE_QUEUE.put_nowait(video_id)
    return len(chunks)


def pause_job(video_id: str):
    PAUSE_FLAGS[video_id] = True
    log(video_id, "Foydalanuvchi to'xtatishni so'radi (xavfsiz nuqtada to'xtaydi).")


def _reset_transcription_to_segments_ready(video_id: str, message: str):
    """Transkripsiyani bekor qilingandan keyin videoni 'segments_ready' holatiga
    qaytaradi - shunda 'Til' tanlash oynasi qayta chiqadi va foydalanuvchi TO'G'RI
    tilni tanlab, transkripsiyani boshidan boshlashi mumkin. Ilgari bu yerda status
    'cancelled' qilib qo'yilardi - bu holat frontendda umuman ishlov berilmagan
    ("tasdiqlangan" bo'limiga tasodifan tushib qolardi) va backend ham
    /transcribe so'rovini qabul qilmasdi (faqat 'segments_ready'/'transcription_ready'
    ruxsat etilgan) - natijada video umuman qayta ishlatib bo'lmaydigan holatda
    qotib qolardi. Bo'laklar (chunks) o'zi bo'laklashda yaratilgani uchun qayta
    saqlanadi - faqat ularning transkripsiya natijasi tozalanadi."""
    db.execute("UPDATE chunks SET status = 'pending', transcript = NULL, error = NULL WHERE video_id = ?",
               (video_id,))
    _update_video(video_id, status="segments_ready", blocked_reason=None, progress=0, message=message, error=None,
                  repetition_chunk_index=None, repetition_info=None)


def cancel_job(video_id: str):
    CANCEL_FLAGS[video_id] = True
    video = db.fetchone("SELECT status, blocked_reason FROM videos WHERE id = ?", (video_id,))
    if video and video["status"] == "transcribing" and not video["blocked_reason"]:
        # Hozir faol ishlayotgan bo'laklar bor - ular xavfsiz nuqtada to'xtaguncha
        # kutamiz. run_transcription_job() CANCEL_FLAGS'ni ko'rib, o'zi
        # 'segments_ready'ga qaytaradi (pastda, jarayon tugagach).
        log(video_id, "Bekor qilish so'raldi - joriy bo'laklar tugagach 'Bo'laklar tayyor' holatiga qaytariladi.")
        return
    # Allaqachon to'xtatilgan (pauza qilingan) yoki boshqa xato bilan bloklangan -
    # faol run_transcription_job() yo'q, shuning uchun bu yerning o'zida darhol qaytaramiz.
    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)
    _reset_transcription_to_segments_ready(
        video_id, "Bekor qilindi. Tilni qayta tanlab, transkripsiyani qaytadan boshlashingiz mumkin.")
    log(video_id, "Bekor qilindi - 'Bo'laklar tayyor' holatiga qaytarildi.")


def _effective_chunk_language(video: dict, chunk: dict) -> str:
    """Bo'lak uchun ATAYLAB o'rnatilgan til (chunks.language, None bo'lmasa - bo'sh
    satr ham "ataylab avtomatik" hisoblanadi) bo'lsa o'shani, aks holda videoning
    umumiy tilini qaytaradi. Shu funksiya orqali har bir bo'lak (jumladan qayta
    yuborilgan bo'laklar) O'ZINING tilida ishlanadi, butun ish uchun bitta umumiy
    til/prompt emas."""
    chunk_language = chunk.get("language") if isinstance(chunk, dict) else chunk["language"]
    if chunk_language is not None:
        return chunk_language
    return video["language"] or ""


async def _transcribe_chunk_in_pieces(client, chunk: dict, api_key: str, language: str, prompt: str,
                                       piece_seconds: int = 45):
    """Qayta ishlashda (retry) aniqlikni oshirish uchun bo'lak audiosini kichik
    vaqtinchalik qismlarga bo'lib, har birini alohida Whisper'ga yuboradi, so'ng
    vaqt kodlarini to'g'ri qo'shib bo'lak-darajasidagi bitta natijaga birlashtiradi.
    Bo'lish yoki istalgan qism muvaffaqiyatsiz bo'lsa None qaytaradi - chaqiruvchi
    tomon shunda butun bo'lakni yagona so'rov bilan (oddiy usulda) qayta urinadi,
    ya'ni bu funksiya hech qachon retry jarayonini butunlay to'xtatmaydi."""
    chunk_path = Path(chunk["path"])
    work_dir = CHUNKS_DIR / chunk["video_id"] / f"retry_{chunk['id']}"
    shutil.rmtree(work_dir, ignore_errors=True)
    try:
        loop = asyncio.get_event_loop()
        pieces = await loop.run_in_executor(
            None, transcription.split_audio_into_pieces, chunk_path, work_dir, piece_seconds)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        return None

    all_segments = []
    detected_langs = []
    cumulative = 0.0
    try:
        for piece_path, piece_duration in pieces:
            data = await transcription.transcribe_chunk_via_api(client, piece_path, api_key, language, prompt)
            for s in data.get("segments", []):
                all_segments.append({
                    "start": float(s.get("start", 0)) + cumulative,
                    "end": float(s.get("end", 0)) + cumulative,
                    "text": (s.get("text") or "").strip(),
                    "no_speech_prob": s.get("no_speech_prob"),
                    "avg_logprob": s.get("avg_logprob"),
                })
            if data.get("language"):
                detected_langs.append(data["language"])
            cumulative += piece_duration
    except Exception:
        return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    detected_lang = max(set(detected_langs), key=detected_langs.count) if detected_langs else ""
    return {"segments": all_segments, "language": detected_lang}


async def _process_one_chunk(client, video, chunk, lock, ctx):
    if CANCEL_FLAGS.get(video["id"]) or PAUSE_FLAGS.get(video["id"]) or ctx["stop"]:
        return
    db.execute("UPDATE chunks SET status = 'running', updated_at = ? WHERE id = ?", (db.now(), chunk["id"]))

    effective_language = _effective_chunk_language(video, chunk)
    prompt = transcription.build_prompt(effective_language, video["instruction"] or "",
                                         group=video["topic_group"] or None)
    use_split = bool(chunk.get("force_split"))
    # Bir martalik bayroq - shu urinishdan keyin iste'mol qilinadi (keyingi oddiy
    # qayta ishlashlar, masalan "vaqt oralig'ini qayta ishlash", uni qayta yoqmaydi;
    # faqat "Bo'lakni qayta yubor" tugmasi uni qayta 1 ga o'rnatadi).
    db.execute("UPDATE chunks SET force_split = 0 WHERE id = ?", (chunk["id"],))

    tried_key_ids = set()
    generic_attempts = 0
    last_err = None
    data = None
    used_key_id = None

    for _ in range(10):
        if not keys_manager.has_any_active_key():
            async with lock:
                db.execute("UPDATE chunks SET status = 'pending', updated_at = ? WHERE id = ?",
                           (db.now(), chunk["id"]))
                _update_video(video["id"], blocked_reason="api_key",
                               message="Ishlaydigan OpenAI API kalit topilmadi. Yangi API kalit kiriting.")
                log(video["id"], "TO'XTATILDI: aktiv API kalit yo'q.")
                ctx["stop"] = True
            return
        kid, raw_key = keys_manager.get_next_active_key(exclude_ids=tried_key_ids)
        if kid is None:
            async with lock:
                db.execute("UPDATE chunks SET status = 'pending', updated_at = ? WHERE id = ?",
                           (db.now(), chunk["id"]))
                _update_video(video["id"], blocked_reason="api_key",
                               message="Barcha API kalitlar xato qaytardi. Yangi API kalit kiriting yoki tekshiring.")
                log(video["id"], "TO'XTATILDI: barcha kalitlar sinovdan o'tkazildi, hech biri ishlamadi.")
                ctx["stop"] = True
            return
        try:
            if use_split:
                data = await _transcribe_chunk_in_pieces(client, chunk, raw_key, effective_language, prompt)
                if data is None:
                    # Kichik qismlarga bo'lish yoki ulardan biri muvaffaqiyatsiz bo'ldi -
                    # butun bo'lakni yagona so'rov bilan (oddiy usulda) qayta urinamiz.
                    data = await transcription.transcribe_chunk_via_api(
                        client, Path(chunk["path"]), raw_key, effective_language, prompt)
            else:
                data = await transcription.transcribe_chunk_via_api(
                    client, Path(chunk["path"]), raw_key, effective_language, prompt)
            keys_manager.mark_result(kid, True)
            used_key_id = kid
            break
        except Exception as e:
            last_err = e
            if transcription.is_key_error(e):
                keys_manager.mark_result(kid, False, str(e))
                tried_key_ids.add(kid)
                async with lock:
                    log(video["id"], f"Bo'lak {chunk['chunk_index']+1}: kalit xatosi, keyingi kalitga o'tilmoqda...")
                continue
            else:
                generic_attempts += 1
                if generic_attempts >= 3:
                    break
                await asyncio.sleep(2 ** generic_attempts)
                continue

    if data is None:
        async with lock:
            raw_msg = str(last_err) if last_err else "Noma'lum xato"
            err_msg = transcription.classify_chunk_error(last_err) if last_err else raw_msg
            db.execute("UPDATE chunks SET status = 'error', error = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                       (err_msg[:500], db.now(), chunk["id"]))
            log(video["id"], f"XATO (bo'lak {chunk['chunk_index']+1}): {err_msg}")
        return

    offset = chunk["start_time"]
    segs = [
        {"start": float(s.get("start", 0)) + offset, "end": float(s.get("end", 0)) + offset,
         "text": (s.get("text") or "").strip()}
        for s in data.get("segments", []) if (s.get("text") or "").strip()
    ]
    detected_lang = data.get("language", "") or ""
    issues = transcription.assess_segment_issues(data.get("segments", []), offset, expected_language=effective_language or "")

    async with lock:
        db.execute(
            "UPDATE chunks SET status = 'completed', transcript = ?, error = NULL, updated_at = ? WHERE id = ?",
            (json.dumps({"lang": detected_lang, "segments": segs, "issues": issues}, ensure_ascii=False),
             db.now(), chunk["id"]),
        )
        total = db.fetchone("SELECT COUNT(*) c FROM chunks WHERE video_id = ?", (video["id"],))["c"]
        completed = db.fetchone("SELECT COUNT(*) c FROM chunks WHERE video_id = ? AND status = 'completed'",
                                 (video["id"],))["c"]
        progress = round(completed / total * 100, 1) if total else 0
        issue_note = f" ({len(issues)} ta shubhali joy)" if issues else ""
        _update_video(video["id"], progress=progress, message=f"{completed}/{total} bo'lak tayyor.")
        log(video["id"], f"Bo'lak {chunk['chunk_index']+1}/{total} tayyor{issue_note}.")
        db.add_cost(video["id"], "transcription",
                     transcription.estimate_whisper_cost(chunk["end_time"] - chunk["start_time"]),
                     detail=f"bo'lak {chunk['chunk_index']+1} (Whisper)")


async def run_transcription_job(video_id: str):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video or video["status"] == "cancelled":
        return
    _update_video(video_id, blocked_reason=None)
    pending_chunks = db.fetchall(
        "SELECT * FROM chunks WHERE video_id = ? AND status = 'pending' ORDER BY chunk_index ASC", (video_id,))

    if pending_chunks:
        sem = asyncio.Semaphore(MAX_WHISPER_CONCURRENCY)
        lock = asyncio.Lock()
        ctx = {"stop": False}

        async def bound_worker(chunk):
            async with sem:
                RUNNING_CHUNK_TASKS[chunk["id"]] = asyncio.current_task()
                CHUNK_STARTED_AT[chunk["id"]] = time.time()
                try:
                    await _process_one_chunk(client, video, chunk, lock, ctx)
                except asyncio.CancelledError:
                    async with lock:
                        db.execute("UPDATE chunks SET status = 'pending', updated_at = ? WHERE id = ?",
                                   (db.now(), chunk["id"]))
                        log(video["id"], f"Bo'lak {chunk['chunk_index']+1}: foydalanuvchi bekor qildi, "
                                          f"qayta navbatga qo'yildi.")
                finally:
                    RUNNING_CHUNK_TASKS.pop(chunk["id"], None)
                    CHUNK_STARTED_AT.pop(chunk["id"], None)

        async with httpx.AsyncClient(timeout=600) as client:
            await asyncio.gather(*(bound_worker(c) for c in pending_chunks))

    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if video["blocked_reason"] == "api_key":
        PAUSE_FLAGS.pop(video_id, None)
        return
    if CANCEL_FLAGS.get(video_id):
        _reset_transcription_to_segments_ready(
            video_id, "Bekor qilindi. Tilni qayta tanlab, transkripsiyani qaytadan boshlashingiz mumkin.")
        CANCEL_FLAGS.pop(video_id, None)
        log(video_id, "Bekor qilindi - 'Bo'laklar tayyor' holatiga qaytarildi.")
        return
    if PAUSE_FLAGS.get(video_id):
        _update_video(video_id, blocked_reason="paused", message="To'xtatildi (xavfsiz nuqtada).")
        PAUSE_FLAGS.pop(video_id, None)
        log(video_id, "To'xtatildi (foydalanuvchi so'rovi).")
        return

    remaining = db.fetchone("SELECT COUNT(*) c FROM chunks WHERE video_id = ? AND status != 'completed'",
                             (video_id,))["c"]
    error_count = db.fetchone("SELECT COUNT(*) c FROM chunks WHERE video_id = ? AND status = 'error'",
                               (video_id,))["c"]
    if remaining == 0:
        await finalize_results(video_id)
    elif error_count > 0:
        _update_video(video_id, blocked_reason="chunk_errors",
                       message=f"{error_count} ta bo'lakda xato yuz berdi. Qayta urinib ko'ring.")
        log(video_id, f"Jarayon tugadi, lekin {error_count} ta bo'lak xato bilan yakunlandi.")


async def finalize_results(video_id: str):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    chunks = db.fetchall("SELECT * FROM chunks WHERE video_id = ? ORDER BY chunk_index ASC", (video_id,))

    all_segments = []
    lang_votes = {}
    for c in chunks:
        if not c["transcript"]:
            continue
        payload = json.loads(c["transcript"])
        lang = payload.get("lang") or ""
        if lang:
            lang_votes[lang] = lang_votes.get(lang, 0) + 1
        all_segments.extend(payload.get("segments", []))
    all_segments.sort(key=lambda s: s["start"])

    detected_lang = video["language"] or (max(lang_votes, key=lang_votes.get) if lang_votes else "")
    variants = transcription.variants_for_language(detected_lang)

    loop = asyncio.get_event_loop()
    final_segments = await loop.run_in_executor(
        None, transcription.correct_segments_with_glossary, all_segments, variants
    )
    txt_text = transcription.build_txt(final_segments)

    flagged_issues = []
    for c in chunks:
        if not c["transcript"]:
            continue
        payload = json.loads(c["transcript"])
        for issue in payload.get("issues", []):
            flagged_issues.append({**issue, "chunk_index": c["chunk_index"]})
    flagged_issues.sort(key=lambda i: i["start"])

    # Agar bu bo'lak QAYTA ishlangandan keyingi jamlash bo'lsa (video allaqachon
    # tarjima qilingan edi) va segmentlar soni o'zgargan bo'lsa, eski tarjima
    # endi noto'g'ri joylarga mos kelib qolishi mumkin (tarjima segmentlari
    # original bilan INDEKS orqali bog'langan) - shuning uchun xavfsizlik uchun
    # tarjima tozalanadi va foydalanuvchiga aniq sabab bilan qayta tarjima
    # qilish kerakligi bildiriladi.
    had_translation = (video["translation_status"] or "none") in ("ready", "uploaded", "pasted", "generating")
    old_translation_count = len(json.loads(video["translation_segments"] or "[]"))
    translation_mismatch = had_translation and old_translation_count != len(final_segments)

    extra_fields = {}
    if translation_mismatch:
        extra_fields = {
            "translation_status": "none", "translation_text": "", "translation_segments": "[]",
        }

    _update_video(video_id, status="transcription_ready", blocked_reason=None, progress=100,
                  message="Transkripsiya tayyor. Tekshirib tasdiqlang.",
                  detected_language=detected_lang, error=None,
                  transcript_text=txt_text,
                  transcript_segments=json.dumps(final_segments, ensure_ascii=False),
                  flagged_issues=json.dumps(flagged_issues, ensure_ascii=False),
                  **extra_fields)
    write_transcript_results(video_id)
    if translation_mismatch:
        log(video_id, "OGOHLANTIRISH: bo'lak qayta ishlangandan keyin segmentlar soni o'zgardi - "
                       "eski tarjima endi original matn bilan mos kelmasligi mumkin edi, shuning uchun "
                       "xavfsizlik uchun tozalandi. Original matnni qaytadan tasdiqlab, tarjimani qaytadan yarating.")
    elif had_translation:
        log(video_id, "Diqqat: bo'lak qayta ishlandi, video allaqachon tarjima qilingan edi - "
                       "o'zgargan qismning tarjimasini \"Tahrirlash va audio\" bo'limidan tekshirib chiqing.")
    issue_note = f" {len(flagged_issues)} ta shubhali joy topildi." if flagged_issues else ""
    log(video_id, f"Yakunlandi. Jami {len(final_segments)} ta segment.{issue_note} Natijalar saqlandi.")


def write_transcript_results(video_id: str):
    """Original matn (transkripsiya)dan SRT/TXT/VTT natija fayllarini yozadi -
    ilk yakunlashda ham, qo'lda tahrirlashdan keyin ham shu funksiya ishlatiladi."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    segments = json.loads(video["transcript_segments"] or "[]")
    if not segments:
        return
    srt_text = transcription.build_srt(segments)
    txt_text = transcription.build_txt(segments)
    vtt_text = transcription.build_vtt(segments)

    out_dir = RESULTS_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(Path(video["original_name"]).stem) or "natija"
    srt_path = out_dir / f"{base}.srt"
    txt_path = out_dir / f"{base}.txt"
    vtt_path = out_dir / f"{base}.original.vtt"
    srt_path.write_text(srt_text, encoding="utf-8")
    txt_path.write_text(txt_text, encoding="utf-8")
    vtt_path.write_text(vtt_text, encoding="utf-8")

    db.execute("DELETE FROM results WHERE video_id = ? AND kind IN ('srt','txt','vtt_original')", (video_id,))
    for kind, path in (("srt", srt_path), ("txt", txt_path), ("vtt_original", vtt_path)):
        db.execute(
            "INSERT INTO results (id, video_id, kind, filename, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_id(), video_id, kind, path.name, str(path), db.now()),
        )


def apply_transcript_edits(video_id: str, new_texts: list) -> dict:
    """Original (Whisper) matnni qo'lda tahrirlash - foydalanuvchi xato yozilgan
    bo'lakni qayta Whisper'ga yubormasdan, to'g'ridan-to'g'ri matnini tuzatadi."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Video topilmadi.")
    segments = json.loads(video["transcript_segments"] or "[]")
    if not segments:
        raise ValueError("Original matn segmentlari topilmadi.")
    if len(new_texts) != len(segments):
        raise ValueError(f"Bo'laklar soni mos kelmadi: {len(new_texts)} != {len(segments)}")

    changed_count = 0
    new_segments = []
    for i, s in enumerate(segments):
        new_text = (new_texts[i] or "").strip()
        if new_text != s["text"]:
            changed_count += 1
        new_segments.append({"start": s["start"], "end": s["end"], "text": new_text})

    txt_text = transcription.build_txt(new_segments)
    _update_video(video_id, transcript_text=txt_text,
                  transcript_segments=json.dumps(new_segments, ensure_ascii=False))
    write_transcript_results(video_id)
    log(video_id, f"Original matn qo'lda tahrirlandi: {changed_count} ta bo'lak o'zgardi.")
    return {"changed_count": changed_count}


async def transcribe_consumer():
    while True:
        video_id = await TRANSCRIBE_QUEUE.get()
        try:
            await run_transcription_job(video_id)
        except Exception as e:
            log(video_id, f"XATO (job): {e}\n{traceback.format_exc()[-500:]}")
            _update_video(video_id, blocked_reason="error", error=str(e))
        finally:
            TRANSCRIBE_QUEUE.task_done()


# ---------------------------------------------------------------------------
#                          TASDIQLASH VA TARJIMA
# ---------------------------------------------------------------------------

def approve_transcript(video_id: str):
    _update_video(video_id, transcript_approved=1, status="transcription_approved")
    log(video_id, "Original matn tasdiqlandi.")


async def retranscribe_segment(video_id: str, index: int, language: str = None) -> dict:
    """Bitta aniq segmentni (butun bo'lakni emas) original videodan qayta ajratib,
    qayta Whisper'ga yuboradi - foydalanuvchi tarjimadan norozi bo'lgan joyni, avval
    original matnni yangilab, keyin qayta tarjima qilishi uchun. `language` berilsa
    (None emas), aynan shu til Whisper so'roviga majburiy yuboriladi - berilmasa,
    videoning umumiy tili ishlatiladi (avvalgi xatti-harakat)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Video topilmadi.")
    if not video["path"] or not Path(video["path"]).exists():
        raise ValueError("Original video fayli topilmadi.")
    segments = json.loads(video["transcript_segments"] or "[]")
    if index < 0 or index >= len(segments):
        raise ValueError("Bunday segment mavjud emas.")
    if not keys_manager.has_any_active_key():
        raise ValueError("Ishlaydigan OpenAI API kalit topilmadi. Avval API kalit qo'shing.")

    effective_language = language if language is not None else (video["language"] or "")
    seg = segments[index]
    work_dir = CHUNKS_DIR / video_id / "resegment"
    work_dir.mkdir(parents=True, exist_ok=True)
    clip_path = work_dir / f"seg_{index:05d}.mp3"

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, transcription.extract_audio_slice, Path(video["path"]), seg["start"], seg["end"], clip_path)

    prompt = transcription.build_prompt(effective_language, video["instruction"] or "",
                                         group=video["topic_group"] or None)
    kid, raw_key = keys_manager.get_next_active_key()
    if not raw_key:
        clip_path.unlink(missing_ok=True)
        raise ValueError("Ishlaydigan OpenAI API kalit topilmadi.")

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            data = await transcription.transcribe_chunk_via_api(client, clip_path, raw_key, effective_language, prompt)
        keys_manager.mark_result(kid, True)
    except Exception as e:
        keys_manager.mark_result(kid, False, str(e))
        raise ValueError(transcription.classify_chunk_error(e))
    finally:
        clip_path.unlink(missing_ok=True)

    new_text = " ".join((s.get("text") or "").strip() for s in data.get("segments", [])).strip()
    if not new_text:
        new_text = (data.get("text") or "").strip()

    segments[index] = {"start": seg["start"], "end": seg["end"], "text": new_text}
    txt_text = transcription.build_txt(segments)
    _update_video(video_id, transcript_text=txt_text, transcript_segments=json.dumps(segments, ensure_ascii=False))
    write_transcript_results(video_id)
    db.add_cost(video_id, "transcription", transcription.estimate_whisper_cost(seg["end"] - seg["start"]),
                detail=f"{index + 1}-segmentni qayta Whisper'ga yuborish")

    translation_cleared = False
    translations = json.loads(video["translation_segments"] or "[]")
    if index < len(translations):
        translations[index] = {"start": seg["start"], "end": seg["end"], "text": ""}
        plain = "\n\n".join(t["text"] for t in translations)
        _update_video(video_id, translation_text=plain,
                      translation_segments=json.dumps(translations, ensure_ascii=False))
        write_translation_results(video_id)
        translation_cleared = True

    lang_note = f" (til: {storage_lang_label(effective_language)})" if language is not None else ""
    log(video_id, f"{index + 1}-segment Whisper orqali qayta olindi{lang_note}."
                   f"{' Tarjimasi tozalandi - qayta tarjima qiling.' if translation_cleared else ''}")
    return {"text": new_text, "translation_cleared": translation_cleared}


def replace_chunk_transcript(video_id: str, chunk_id: str, segments: list):
    """Foydalanuvchi tayyorlagan matn/SRT bilan bitta bo'lakning (5 daqiqalik audio qism)
    Whisper natijasini to'liq almashtiradi. `segments` - [{"start","end","text"}] ro'yxati,
    video-darajasidagi (absolute) vaqt bilan bo'lishi shart - chaqiruvchi tomon (app.py)
    kerak bo'lsa bo'lak boshlanish vaqtini qo'shib beradi."""
    chunk = db.fetchone("SELECT * FROM chunks WHERE id = ? AND video_id = ?", (chunk_id, video_id))
    if not chunk:
        raise ValueError("Bo'lak topilmadi.")
    payload = {"lang": "", "segments": segments, "issues": []}
    db.execute("UPDATE chunks SET status = 'completed', transcript = ?, error = NULL, updated_at = ? WHERE id = ?",
               (json.dumps(payload, ensure_ascii=False), db.now(), chunk_id))
    log(video_id, f"Bo'lak {chunk['chunk_index'] + 1} matni qo'lda (yuklangan fayldan) almashtirildi "
                   f"({len(segments)} ta qism).")


def get_translation_memory_context() -> str:
    """Sozlamalarda saqlangan Instruksiya/Kontekst va xotiraga qo'shilgan barcha
    qoidalarni birlashtirib, tarjima so'roviga qo'shish uchun tayyorlaydi."""
    notes = db.fetchall("SELECT content FROM translation_memory_notes ORDER BY created_at ASC")
    if not notes:
        return ""
    return "\n".join(f"- {n['content']}" for n in notes)


async def run_auto_translate(video_id: str, provider: str = "openai"):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    try:
        segments = json.loads(video["transcript_segments"] or "[]")
        if not segments:
            raise RuntimeError("Original matn segmentlari topilmadi.")
        kid, raw = keys_manager.get_next_active_key(provider=provider)
        if not raw:
            provider_label = "Claude" if provider == "claude" else "OpenAI"
            raise RuntimeError(f"Ishlaydigan {provider_label} API kalit topilmadi. Avval API kalit qo'shing.")

        instruction = db.get_setting("translation_instruction", "") or ""
        context = db.get_setting("translation_context", "") or ""
        memory_notes = get_translation_memory_context()
        full_context = "\n\n".join(x for x in (context, memory_notes) if x)

        async with httpx.AsyncClient() as client:
            if provider == "claude":
                translated_texts, usage = await translation.translate_segments_via_claude(
                    client, raw, segments, extra_instructions=instruction, extra_context=full_context)
            else:
                translated_texts, usage = await translation.translate_segments_via_openai(
                    client, raw, segments, extra_instructions=instruction, extra_context=full_context)
        keys_manager.mark_result(kid, True)

        translation_segments = [
            {"start": s["start"], "end": s["end"], "text": t} for s, t in zip(segments, translated_texts)
        ]
        plain = "\n\n".join(translated_texts)
        _update_video(video_id, translation_text=plain,
                      translation_segments=json.dumps(translation_segments, ensure_ascii=False),
                      translation_status="ready", translation_source=f"auto_{provider}", status="translation_ready",
                      blocked_reason=None, error=None, message="Avtomatik tarjima tayyor.")

        if usage:
            input_tok = usage.get("prompt_tokens", 0)
            output_tok = usage.get("completion_tokens", 0)
            cost = translation.estimate_translation_cost(0, 0, provider=provider)
            if provider == "claude":
                cost = round((input_tok / 1_000_000) * 1.0 + (output_tok / 1_000_000) * 5.0, 6)
            else:
                cost = round((input_tok / 1_000_000) * 0.15 + (output_tok / 1_000_000) * 0.60, 6)
        else:
            cost = translation.estimate_translation_cost(
                sum(len(s["text"]) for s in segments), sum(len(t) for t in translated_texts), provider=provider)
        provider_label = "Claude Haiku" if provider == "claude" else "OpenAI gpt-4o-mini"
        db.add_cost(video_id, "translation", cost, detail=f"{provider_label} avtomatik tarjima")
        write_translation_results(video_id)
        log(video_id, f"Avtomatik tarjima tayyor ({provider_label}).")
    except Exception as e:
        _update_video(video_id, translation_status="failed", message=f"Tarjima xatosi: {e}")
        log(video_id, f"XATO (tarjima): {e}\n{traceback.format_exc()[-400:]}")


async def fill_empty_translations(video_id: str, provider: str = "openai"):
    """Faqat matni bo'sh qolgan tarjima bo'laklarini AI orqali to'ldiradi -
    to'liq qayta tarjima qilmaydi, allaqachon mavjud matnlarga tegmaydi."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    try:
        originals = json.loads(video["transcript_segments"] or "[]")
        translations = json.loads(video["translation_segments"] or "[]")
        if not originals or len(originals) != len(translations):
            raise RuntimeError("Tarjima segmentlari original bilan mos emas.")
        empty_indices = [i for i, t in enumerate(translations) if not (t.get("text") or "").strip()]
        if not empty_indices:
            _update_video(video_id, blocked_reason=None, message="Bo'sh bo'lak topilmadi.")
            return
        kid, raw = keys_manager.get_next_active_key(provider=provider)
        if not raw:
            provider_label = "Claude" if provider == "claude" else "OpenAI"
            raise RuntimeError(f"Ishlaydigan {provider_label} API kalit topilmadi. Avval API kalit qo'shing.")

        instruction = db.get_setting("translation_instruction", "") or ""
        context = db.get_setting("translation_context", "") or ""
        memory_notes = get_translation_memory_context()
        full_context = "\n\n".join(x for x in (context, memory_notes) if x)

        to_translate = [originals[i] for i in empty_indices]
        async with httpx.AsyncClient() as client:
            if provider == "claude":
                translated_texts, usage = await translation.translate_segments_via_claude(
                    client, raw, to_translate, extra_instructions=instruction, extra_context=full_context)
            else:
                translated_texts, usage = await translation.translate_segments_via_openai(
                    client, raw, to_translate, extra_instructions=instruction, extra_context=full_context)
        keys_manager.mark_result(kid, True)

        for idx, text in zip(empty_indices, translated_texts):
            translations[idx] = {"start": originals[idx]["start"], "end": originals[idx]["end"], "text": text}
        plain = "\n\n".join(t["text"] for t in translations)
        _update_video(video_id, translation_text=plain,
                      translation_segments=json.dumps(translations, ensure_ascii=False),
                      translation_status="ready", status="translation_ready",
                      blocked_reason=None, error=None,
                      message=f"{len(empty_indices)} ta bo'sh bo'lak avtomatik tarjima qilindi.")

        if usage:
            input_tok = usage.get("prompt_tokens", 0)
            output_tok = usage.get("completion_tokens", 0)
            if provider == "claude":
                cost = round((input_tok / 1_000_000) * 1.0 + (output_tok / 1_000_000) * 5.0, 6)
            else:
                cost = round((input_tok / 1_000_000) * 0.15 + (output_tok / 1_000_000) * 0.60, 6)
        else:
            cost = translation.estimate_translation_cost(
                sum(len(s["text"]) for s in to_translate), sum(len(t) for t in translated_texts), provider=provider)
        provider_label = "Claude Haiku" if provider == "claude" else "OpenAI gpt-4o-mini"
        db.add_cost(video_id, "translation", cost, detail=f"{provider_label}: {len(empty_indices)} bo'sh bo'lak to'ldirildi")
        write_translation_results(video_id)
        log(video_id, f"{len(empty_indices)} ta bo'sh bo'lak avtomatik tarjima qilindi ({provider_label}).")
    except Exception as e:
        _update_video(video_id, blocked_reason="error", message=f"Bo'sh bo'laklarni to'ldirishda xato: {e}")
        log(video_id, f"XATO (bo'sh bo'laklarni to'ldirish): {e}\n{traceback.format_exc()[-400:]}")


def apply_manual_translation(video_id: str, texts: list, source: str):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    segments = json.loads(video["transcript_segments"] or "[]")
    translation_segments = [{"start": s["start"], "end": s["end"], "text": t} for s, t in zip(segments, texts)]
    plain = "\n\n".join(texts)
    _update_video(video_id, translation_text=plain,
                  translation_segments=json.dumps(translation_segments, ensure_ascii=False),
                  translation_status=source, translation_source=source, status="translation_ready",
                  blocked_reason=None, error=None, message="Tarjima qo'shildi.")
    write_translation_results(video_id)
    log(video_id, f"Tarjima qo'lda kiritildi ({source}).")


def apply_direct_srt_translation(video_id: str, segments: list):
    """Foydalanuvchi tayyorlagan SRT faylini o'z vaqt belgilari bilan to'g'ridan-to'g'ri
    tarjima sifatida saqlaydi (original transkripsiya bo'laklar soniga bog'liq emas)."""
    plain = "\n\n".join(s["text"] for s in segments)
    _update_video(video_id, translation_text=plain,
                  translation_segments=json.dumps(segments, ensure_ascii=False),
                  translation_status="uploaded", translation_source="srt_direct", status="translation_ready",
                  blocked_reason=None, error=None, message=f"SRT fayldan {len(segments)} ta bo'lak yuklandi.")
    write_translation_results(video_id)
    log(video_id, f"O'zbekcha SRT to'g'ridan-to'g'ri yuklandi ({len(segments)} ta bo'lak).")


def write_translation_results(video_id: str):
    """O'zbekcha tarjimadan SRT/VTT natija fayllarini yozadi (video sahifasida
    yuklab olish va pleyer subtitle treki uchun)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    segments = json.loads(video["translation_segments"] or "[]")
    if not segments:
        return
    srt_text = transcription.build_srt(segments)
    vtt_text = transcription.build_vtt(segments)
    out_dir = RESULTS_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(Path(video["original_name"]).stem) or "natija"
    srt_path = out_dir / f"{base}.uz.srt"
    vtt_path = out_dir / f"{base}.uz.vtt"
    srt_path.write_text(srt_text, encoding="utf-8")
    vtt_path.write_text(vtt_text, encoding="utf-8")
    db.execute("DELETE FROM results WHERE video_id = ? AND kind IN ('srt_uz', 'vtt_uz')", (video_id,))
    for kind, path in (("srt_uz", srt_path), ("vtt_uz", vtt_path)):
        db.execute(
            "INSERT INTO results (id, video_id, kind, filename, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_id(), video_id, kind, path.name, str(path), db.now()),
        )


def write_final_subtitles(video_id: str, freeze_points: list):
    """Freeze nuqtalari asosida, YAKUNIY (freeze bilan mos, 'uz' audio/video treki
    uchun) SRT/VTT fayllarni yaratadi. MUHIM: manba (source) SRT/VTT fayllarga
    (write_translation_results yozgan) HECH TEGILMAYDI - ular original vaqt bilan
    o'zgarishsiz qoladi. 'Original' video/audio treki hech qachon freeze bilan
    o'zgartirilmaydi, shuning uchun uning subtitri ham doim manba (original.vtt)
    bo'lib qoladi - faqat 'uz' (freeze-rendered yakuniy video) treki uchun
    moslashtirilgan variant kerak."""
    db.execute("DELETE FROM results WHERE video_id = ? AND kind IN ('srt_uz_final', 'vtt_uz_final')",
               (video_id,))
    active = [f for f in (freeze_points or []) if f.get("duration", 0) > 0.05]
    if not active:
        return
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    translation_segments = json.loads(video["translation_segments"] or "[]")
    if not translation_segments:
        return

    adjusted = transcription.apply_freeze_to_segments(translation_segments, active)
    srt_text = transcription.build_srt(adjusted)
    vtt_text = transcription.build_vtt(adjusted)

    out_dir = RESULTS_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(Path(video["original_name"]).stem) or "natija"
    srt_path = out_dir / f"{base}.uz.final.srt"
    vtt_path = out_dir / f"{base}.uz.final.vtt"
    srt_path.write_text(srt_text, encoding="utf-8")
    vtt_path.write_text(vtt_text, encoding="utf-8")
    for kind, path in (("srt_uz_final", srt_path), ("vtt_uz_final", vtt_path)):
        db.execute(
            "INSERT INTO results (id, video_id, kind, filename, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_id(), video_id, kind, path.name, str(path), db.now()),
        )
    log(video_id, f"Yakuniy (freeze bilan moslashtirilgan) o'zbekcha subtitr fayllar yaratildi "
                   f"({len(active)} ta freeze nuqtasi, jami {transcription.total_freeze_duration(active):.2f}s siljish).")


def get_translation_blocks(video_id: str) -> list:
    """Har bir original segment va unga mos tarjima bo'lagini indeks bilan qaytaradi
    ('Tahrirlash va audio' bo'limi uchun)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    originals = json.loads(video["transcript_segments"] or "[]")
    translations = json.loads(video["translation_segments"] or "[]")
    audio_status_by_index = {}
    if video["tts_job_id"]:
        segs = db.fetchall("SELECT seg_index, status, error FROM tts_segments WHERE job_id = ?",
                            (video["tts_job_id"],))
        audio_status_by_index = {s["seg_index"]: {"status": s["status"], "error": s["error"]} for s in segs}
    blocks = []
    for i, orig in enumerate(originals):
        tr = translations[i] if i < len(translations) else None
        blocks.append({
            "index": i, "start": orig["start"], "end": orig["end"],
            "original_text": orig["text"], "translation_text": tr["text"] if tr else "",
            "audio": audio_status_by_index.get(i),
        })
    return blocks


def apply_block_edits(video_id: str, new_texts: list):
    """Faqat o'zgargan bo'laklarni qayta ishlash uchun belgilaydi - o'zgarmagan
    bo'laklarning tayyor audiosi saqlanib qoladi (§10-13)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    originals = json.loads(video["transcript_segments"] or "[]")
    old_translations = json.loads(video["translation_segments"] or "[]")
    if len(new_texts) != len(originals):
        raise ValueError(f"Bo'laklar soni mos kelmadi: {len(new_texts)} != {len(originals)}")

    changed_indices = []
    new_translation_segments = []
    for i, orig in enumerate(originals):
        old_text = old_translations[i]["text"] if i < len(old_translations) else None
        new_text = new_texts[i]
        new_translation_segments.append({"start": orig["start"], "end": orig["end"], "text": new_text})
        if old_text != new_text:
            changed_indices.append(i)

    plain = "\n\n".join(t["text"] for t in new_translation_segments)
    _update_video(video_id, translation_text=plain,
                  translation_segments=json.dumps(new_translation_segments, ensure_ascii=False))
    write_translation_results(video_id)
    log(video_id, f"Tarjima tahrirlandi: {len(changed_indices)} ta bo'lak o'zgardi.")

    if not changed_indices:
        return {"changed_count": 0, "audio_requeued": False}

    if video["tts_job_id"]:
        for i in changed_indices:
            db.execute("UPDATE tts_segments SET text = ?, status = 'pending', audio_path = NULL, "
                       "cache_key = NULL, error = NULL WHERE job_id = ? AND seg_index = ?",
                       (new_texts[i], video["tts_job_id"], i))
        db.execute("UPDATE tts_jobs SET status = 'queued', error = NULL WHERE id = ?", (video["tts_job_id"],))
        _update_video(video_id, status="audio_processing", blocked_reason=None, audio_status="generating",
                      message=f"{len(changed_indices)} ta bo'lak uchun audio qayta yaratilmoqda...")
        import tts
        tts.TTS_QUEUE.put_nowait(video["tts_job_id"])
        log(video_id, f"{len(changed_indices)} ta o'zgargan bo'lak uchun audio qayta navbatga qo'yildi.")
        return {"changed_count": len(changed_indices), "audio_requeued": True}

    return {"changed_count": len(changed_indices), "audio_requeued": False}


# ---------------------------------------------------------------------------
#                          YAKUNIY VIDEO YIG'ISH (RENDER)
# ---------------------------------------------------------------------------

def enqueue_render(video_id: str) -> bool:
    """Yakuniy video yig'ishni navbatga qo'yadi. Video uchun bu ish ALLAQACHON
    faol ishlayotgan bo'lsa (status='video_rendering' va xato/to'xtagan emas),
    qayta navbatga qo'ymaydi va False qaytaradi - bitta video uchun parallel
    ikkita render ishlashining oldini olish uchun."""
    video = db.fetchone("SELECT status, blocked_reason FROM videos WHERE id = ?", (video_id,))
    if video and video["status"] == "video_rendering" and not video["blocked_reason"]:
        return False
    _update_video(video_id, status="video_rendering", blocked_reason=None,
                  message="Video yig'ilmoqda...", error=None)
    log(video_id, "Video yig'ish navbatga qo'yildi.")
    RENDER_QUEUE.put_nowait(video_id)
    return True


async def render_video(video_id: str):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    tmp_out_path = None
    try:
        if not video["audio_path"] or not Path(video["audio_path"]).exists():
            raise RuntimeError("Audio fayl topilmadi. Avval audio yarating.")
        audio_duration = transcription.get_duration_seconds(Path(video["audio_path"]))
        if audio_duration < 1.0:
            raise RuntimeError(
                f"Audio fayl bo'sh yoki juda qisqa ({audio_duration:.2f}s). "
                f"'Audio' bosqichida audio faylni qayta yarating."
            )
        out_dir = RESULTS_DIR / video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        base = safe_name(Path(video["original_name"]).stem) or "video"
        out_path = out_dir / f"{base}_yakuniy.mp4"
        # Avval VAQTINCHALIK faylga yig'iladi - ffmpeg muvaffaqiyatli tugab,
        # natija tekshirilgandan keyingina yakuniy joyga ko'chiriladi. Shu bilan
        # qayta yig'ish (masalan "Videoni qayta yig'ish") o'rtada xato bilan
        # to'xtasa ham, avvalgi sog'lom yakuniy video hech qachon yarim
        # buzilgan holatda qolmaydi/almashtirilmaydi.
        tmp_out_path = out_dir / f"{base}_yakuniy.rendering.mp4"
        tmp_out_path.unlink(missing_ok=True)

        freeze_points = json.loads(video["freeze_points"]) if video["freeze_points"] else []
        active_freeze_points = [f for f in freeze_points if f.get("duration", 0) > 0.05]
        if active_freeze_points:
            log(video_id, f"Video yig'ilmoqda: {len(active_freeze_points)} ta joyda audio uzunroq, "
                           f"video shu nuqtalarda kutib turadi.")
        else:
            log(video_id, "Video va audio ffmpeg orqali birlashtirilmoqda (fayl hajmiga qarab bir necha "
                           "daqiqa vaqt olishi mumkin)...")
        freeze_work_dir = CHUNKS_DIR / video_id / "freeze_work"

        # Yakuniy fayl aniq shu davomiylikda chiqishi kerak: asl video davomiyligi +
        # freeze'lar yig'indisi. Bu "-shortest" o'rniga "-t" bilan ishlatiladi -
        # qisqaroq audio videoni kesib qo'ymaydi, va ortiqcha uzunlikdan ham himoya qiladi.
        video_duration = float(video["duration"] or 0) or transcription.get_duration_seconds(Path(video["path"]))
        target_duration = None
        if video_duration and video_duration > 0:
            target_duration = video_duration + transcription.total_freeze_duration(active_freeze_points)
            log(video_id, f"DEBUG render: video_duration={video_duration:.3f}s "
                           f"freeze_total={transcription.total_freeze_duration(active_freeze_points):.3f}s "
                           f"target_duration={target_duration:.3f}s")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, transcription.mux_video_audio_with_freezes,
            Path(video["path"]), Path(video["audio_path"]), tmp_out_path, freeze_points, freeze_work_dir,
            target_duration)

        tmp_out_path.replace(out_path)  # atomik ko'chirish - endigina yakuniy video hisoblanadi
        _update_video(video_id, status="completed", blocked_reason=None, final_video_status="ready",
                      final_video_path=str(out_path), message="Yakuniy video tayyor.", error=None)
        log(video_id, "Yakuniy video tayyor.")
    except Exception as e:
        if tmp_out_path:
            tmp_out_path.unlink(missing_ok=True)
        _update_video(video_id, blocked_reason="error", final_video_status="error", error=str(e),
                      message="Video yig'ishda xato.")
        log(video_id, f"XATO (render): {e}\n{traceback.format_exc()[-400:]}")


async def render_consumer():
    while True:
        video_id = await RENDER_QUEUE.get()
        try:
            await render_video(video_id)
        except Exception as e:
            log(video_id, f"XATO (render consumer): {e}\n{traceback.format_exc()[-500:]}")
        finally:
            RENDER_QUEUE.task_done()


# ---------------------------------------------------------------------------
#                          AUDIO JOB YAKUNLANGANDA VIDEONI YANGILASH
# ---------------------------------------------------------------------------

def sync_video_from_tts_job(job_id: str):
    """tts.py chaqiradi: TTS ish holati o'zgarganda bog'langan videoni yangilaydi.
    Faqat video hozir aynan shu ishga bog'langan bo'lsagina yangilanadi - aks holda
    eski (allaqachon almashtirilgan) ish yangi natijani bosib qo'yishi mumkin edi."""
    job = db.fetchone("SELECT * FROM tts_jobs WHERE id = ?", (job_id,))
    if not job or not job["video_id"]:
        return
    video_id = job["video_id"]
    video = db.fetchone("SELECT tts_job_id FROM videos WHERE id = ?", (video_id,))
    if not video or video["tts_job_id"] != job_id:
        log(video_id, f"Eski audio ish ({job_id}) tugadi, lekin video endi boshqa ishga bog'langan - e'tiborsiz qoldirildi.")
        return
    if job["status"] == "completed":
        freeze_points = []
        if job["freeze_points"]:
            try:
                freeze_points = json.loads(job["freeze_points"])
            except Exception:
                pass
        freeze_count = len([f for f in freeze_points if f.get("duration", 0) > 0.05])
        message = "Audio tayyor."
        if freeze_count:
            message = f"Audio tayyor. {freeze_count} ta joyda yakuniy video 'kutib turadi' (audio uzunroq chiqdi)."
        _update_video(video_id, status="audio_ready", blocked_reason=None, audio_status="ready",
                      audio_path=job["result_path"], freeze_points=job["freeze_points"], message=message)
        write_final_subtitles(video_id, freeze_points)
        log(video_id, f"Audio tayyor (TTS ishi yakunlandi).{' ' + str(freeze_count) + ' ta muzlatish nuqtasi.' if freeze_count else ''}")

        # Barcha audio segmentlari muvaffaqiyatli tayyor bo'lgani uchun (shu yerga
        # faqat merge_job() muvaffaqiyatli tugaganda kelinadi) - foydalanuvchi
        # "Video yig'ish"ni bosishini kutmasdan, yakuniy videoni avtomatik
        # navbatga qo'yamiz. Original video fayli va umumiy audio mavjudligini
        # oldindan tekshiramiz; boshqa render allaqachon ketayotgan bo'lsa
        # enqueue_render o'zi qayta navbatga qo'ymaydi.
        full_video = db.fetchone("SELECT path FROM videos WHERE id = ?", (video_id,))
        if full_video and full_video["path"] and Path(full_video["path"]).exists() and job["result_path"]:
            log(video_id, "Barcha audio segmentlari tayyor - yakuniy video avtomatik yig'ishga navbatga qo'yildi.")
            enqueue_render(video_id)
        else:
            log(video_id, "OGOHLANTIRISH: audio tayyor bo'ldi, lekin original video fayli topilmadi - "
                           "avtomatik video yig'ish boshlanmadi. \"Videoni qayta yig'ish\"ni qo'lda urinib ko'ring.")
    elif job["status"] == "paused_api_key":
        _update_video(video_id, blocked_reason="api_key", audio_status="error",
                      message="Audio yaratishda: ishlaydigan OpenAI API kalit topilmadi.")
    elif job["status"] == "error":
        _update_video(video_id, blocked_reason="error", audio_status="error",
                      message=f"Audio yaratishda xato: {job['error'] or ''}")
    elif job["status"] == "cancelled":
        _update_video(video_id, audio_status="error", message="Audio yaratish bekor qilindi.")


# ---------------------------------------------------------------------------
#                          SERVER RESTART - QAYTA TIKLASH
# ---------------------------------------------------------------------------

async def recover_and_start():
    """Server ishga tushganda: uzilib qolgan joblarni xavfsiz holatga o'tkazadi
    va navbatlarga qayta qo'yadi, keyin worker consumer'larni ishga tushiradi.
    Foydalanuvchi ataylab to'xtatgan (blocked_reason mavjud) ishlarga tegilmaydi -
    ular "Davom ettirish" bilan qo'lda davom ettiriladi."""
    interrupted_segmenting = db.fetchall(
        "SELECT id FROM videos WHERE status = 'segmenting' AND blocked_reason IS NULL")
    for v in interrupted_segmenting:
        log(v["id"], "Server qayta ishga tushdi - segmentatsiya qayta boshlanadi.")
        SEGMENT_QUEUE.put_nowait(v["id"])

    interrupted_transcribing = db.fetchall(
        "SELECT id FROM videos WHERE status = 'transcribing' AND blocked_reason IS NULL")
    for v in interrupted_transcribing:
        db.execute("UPDATE chunks SET status = 'pending' WHERE video_id = ? AND status = 'running'", (v["id"],))
        _update_video(v["id"], message="Server qayta ishga tushdi, navbatga qaytarildi.")
        log(v["id"], "Server qayta ishga tushdi - job navbatga qaytarildi.")
        TRANSCRIBE_QUEUE.put_nowait(v["id"])

    interrupted_render = db.fetchall(
        "SELECT id FROM videos WHERE status = 'video_rendering' AND blocked_reason IS NULL")
    for v in interrupted_render:
        log(v["id"], "Server qayta ishga tushdi - video yig'ish qayta boshlanadi.")
        RENDER_QUEUE.put_nowait(v["id"])

    for _ in range(MAX_ACTIVE_VIDEO_JOBS):
        asyncio.create_task(segment_consumer())
        asyncio.create_task(transcribe_consumer())
        asyncio.create_task(render_consumer())

    import tts
    await tts.recover_and_start()
