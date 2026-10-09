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
# (video_id, provider) juftliklarini qabul qiladi - video 'completed' bo'lgach
# qo'shimcha provayder bilan yaratilgan track uchun (asosiy RENDER_QUEUE'dan
# ALOHIDA, chunki u faqat video_id oladi va asosiy videoni yangilaydi).
TRACK_RENDER_QUEUE: asyncio.Queue = asyncio.Queue()
# video_id'larni qabul qiladi - "Ruscha o'rganish" rejimi uchun (foydalanuvchi
# qo'lda yuklagan Learning SRT asosida) yaratiladigan MUSTAQIL yakuniy video.
# Provider juftligisiz, chunki learning_tracks video_id bo'yicha yagona.
LEARNING_RENDER_QUEUE: asyncio.Queue = asyncio.Queue()
# ("intro" | "export", video_id) - Learning intro va so'zlar kuydirilgan
# yuklab olinadigan Learning videosi. Bitta consumer ketma-ket ishlaydi:
# intro tugagach navbatga qo'yilgan eksport undan keyin bajariladi.
LEARNING_EXTRA_QUEUE: asyncio.Queue = asyncio.Queue()
# (video_id, provider) juftliklarini qabul qiladi - provider=None bo'lsa
# ASOSIY yakuniy videoga, aks holda shu provayderning QO'SHIMCHA trekiga
# subtitr "kuydirish" (hardsub) so'ralgan.
SUBTITLE_BURN_QUEUE: asyncio.Queue = asyncio.Queue()

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
    learning = db.fetchone("SELECT tts_job_id FROM learning_tracks WHERE video_id = ?", (video_id,))
    if learning and learning["tts_job_id"]:
        learning_job = db.fetchone("SELECT status FROM tts_jobs WHERE id = ?", (learning["tts_job_id"],))
        if learning_job and learning_job["status"] in ("running", "queued"):
            raise ValueError(
                "Learning audio hozir faol yaratilmoqda - loyihani qayta boshlashdan oldin "
                "jarayon tugashini kuting."
            )

    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)

    cleanup_learning_track(video_id, delete_record=True)

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


def _clear_tts_and_final_video(video: dict):
    """Bog'langan TTS ishini (agar bor bo'lsa) va yakuniy render qilingan
    videoni butunlay o'chiradi - transkripsiya/tarjima/audio bosqichlaridan
    QAYSI biridan qayta boshlansa ham, undan KEYINGI hamma narsa (audio,
    yakuniy video) endi eskirgan hisoblanadi va saqlanishi mumkin emas."""
    video_id = video["id"]
    if video["tts_job_id"]:
        from storage import TTS_DIR
        import tts as tts_module
        tts_module.PAUSE_FLAGS.pop(video["tts_job_id"], None)
        tts_module.CANCEL_FLAGS.pop(video["tts_job_id"], None)
        db.execute("DELETE FROM tts_segments WHERE job_id = ?", (video["tts_job_id"],))
        db.execute("DELETE FROM tts_jobs WHERE id = ?", (video["tts_job_id"],))
        shutil.rmtree(TTS_DIR / video["tts_job_id"], ignore_errors=True)
    if video["final_video_path"]:
        Path(video["final_video_path"]).unlink(missing_ok=True)
    out_dir = RESULTS_DIR / video_id
    if out_dir.exists():
        for p in out_dir.glob("*_yakuniy.rendering.mp4"):
            p.unlink(missing_ok=True)


def _delete_result_kinds(video_id: str, kinds: list):
    """results jadvalidan berilgan 'kind'larga mos yozuvlarni (va ularning
    diskdagi fayllarini) o'chiradi - boshqa kind'larga (masalan original
    transkripsiya fayllariga) tegilmaydi."""
    placeholders = ",".join("?" for _ in kinds)
    rows = db.fetchall(f"SELECT * FROM results WHERE video_id = ? AND kind IN ({placeholders})",
                        (video_id, *kinds))
    for r in rows:
        Path(r["path"]).unlink(missing_ok=True)
    db.execute(f"DELETE FROM results WHERE video_id = ? AND kind IN ({placeholders})", (video_id, *kinds))


_STAGE_ORDER = ("segmentation", "transcription", "translation", "audio", "render")


def reset_from_stage(video_id: str, stage: str):
    """Loyihani berilgan BOSQICHDAN boshlab, undan keyingi hamma narsani
    tozalab, undan oldingi ishni SAQLAB qoladi - ya'ni "Qayta boshlash"ning
    bosqichma-bosqich (cascading) varianti:
      - 'segmentation' - restart_video() bilan bir xil (hech narsa saqlanmaydi,
        faqat original video).
      - 'transcription' - bo'laklar (chunks) saqlanadi, matn+tarjima+audio+video
        tozalanadi. Til qayta tanlab, transkripsiyani boshidan boshlash kerak.
      - 'translation' - original matn (transkripsiya) saqlanadi, tarjima+audio+video
        tozalanadi.
      - 'audio' - tarjima saqlanadi, audio+video tozalanadi.
    ('render' bosqichi uchun alohida funksiya kerak emas - mavjud
    "Videoni qayta yig'ish" tugmasi/enqueue_render() aynan shu ishni qiladi.)
    """
    if stage == "segmentation":
        return restart_video(video_id)
    if stage not in _STAGE_ORDER:
        raise ValueError(f"Noma'lum bosqich: {stage}")

    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Video topilmadi.")
    if video["kind"] != "pipeline":
        raise ValueError("Faqat tarjima loyihalari uchun qayta boshlash mumkin.")

    # Umumiy xavfsizlik tekshiruvi: video hozir biror bosqichda FAOL
    # ishlayotgan bo'lsa, uni bekor qilib bo'lmaydi (fon vazifasi hali ham
    # eski ma'lumot ustida ishlab, tugagach tozalangan holatni bosib qo'yishi
    # mumkin edi - race condition).
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
    if video["translation_status"] == "generating":
        raise ValueError("Tarjima hozir avtomatik yaratilmoqda - avval tugashini kuting.")

    PAUSE_FLAGS.pop(video_id, None)
    CANCEL_FLAGS.pop(video_id, None)

    if stage == "transcription":
        if video["status"] in ("uploaded", "segmenting"):
            raise ValueError(
                "Video hali bo'laklarga bo'linmagan - transkripsiyadan qayta boshlash uchun "
                "avval bo'laklash tugashi kerak."
            )
        _clear_tts_and_final_video(video)
        _delete_result_kinds(video_id, ["srt", "txt", "vtt_original", "srt_uz", "vtt_uz",
                                         "srt_uz_final", "vtt_uz_final"])
        db.execute("UPDATE chunks SET status = 'pending', transcript = NULL, error = NULL WHERE video_id = ?",
                   (video_id,))
        _update_video(
            video_id, status="segments_ready", blocked_reason=None, progress=0,
            message="Transkripsiyadan qaytadan boshlash uchun tozalandi - tilni qayta tanlab boshlang.",
            error=None, language="", instruction="", detected_language="", topic_group=None,
            repetition_chunk_index=None, repetition_info=None,
            transcript_text=None, transcript_segments=None, transcript_approved=0,
            translation_text=None, translation_segments=None, translation_status="none", translation_source=None,
            audio_path=None, audio_status="none", tts_job_id=None,
            final_video_path=None, final_video_status="none", freeze_points=None,
            flagged_issues=None,
        )
        log(video_id, "=== TRANSKRIPSIYADAN QAYTA BOSHLANDI: original matn, tarjima, audio va yakuniy "
                       "video tozalandi. Bo'laklar (chunks) saqlanib qoldi. ===")

    elif stage == "translation":
        if not video["transcript_approved"]:
            raise ValueError("Original matn hali tasdiqlanmagan - avval transkripsiyani tasdiqlang.")
        _clear_tts_and_final_video(video)
        _delete_result_kinds(video_id, ["srt_uz", "vtt_uz", "srt_uz_final", "vtt_uz_final"])
        _update_video(
            video_id, status="transcription_approved", blocked_reason=None,
            message="Tarjimadan qaytadan boshlash uchun tozalandi.", error=None,
            translation_text=None, translation_segments=None, translation_status="none", translation_source=None,
            audio_path=None, audio_status="none", tts_job_id=None,
            final_video_path=None, final_video_status="none", freeze_points=None,
        )
        log(video_id, "=== TARJIMADAN QAYTA BOSHLANDI: tarjima, audio va yakuniy video tozalandi. "
                       "Original matn saqlanib qoldi. ===")

    elif stage == "audio":
        if video["translation_status"] not in ("ready", "uploaded", "pasted"):
            raise ValueError("Tarjima hali tayyor emas - avval tarjimani tayyorlang.")
        _clear_tts_and_final_video(video)
        _delete_result_kinds(video_id, ["srt_uz_final", "vtt_uz_final"])
        _update_video(
            video_id, status="translation_ready", blocked_reason=None,
            message="Audiodan qaytadan boshlash uchun tozalandi.", error=None,
            audio_path=None, audio_status="none", tts_job_id=None,
            final_video_path=None, final_video_status="none", freeze_points=None,
        )
        log(video_id, "=== AUDIODAN QAYTA BOSHLANDI: audio va yakuniy video tozalandi. "
                       "Tarjima saqlanib qoldi. ===")


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
        if not keys_manager.has_any_active_key(owner_id=video["owner_id"]):
            async with lock:
                db.execute("UPDATE chunks SET status = 'pending', updated_at = ? WHERE id = ?",
                           (db.now(), chunk["id"]))
                _update_video(video["id"], blocked_reason="api_key",
                               message="Ishlaydigan OpenAI API kalit topilmadi. Yangi API kalit kiriting.")
                log(video["id"], "TO'XTATILDI: aktiv API kalit yo'q.")
                ctx["stop"] = True
            return
        kid, raw_key = keys_manager.get_next_active_key(exclude_ids=tried_key_ids, owner_id=video["owner_id"])
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
    old_blocks = json.loads(video["translation_segments"] or "[]")
    # Har bir blok necha original segmentni qamrab olganini yig'ib, tarjima
    # generatsiya qilingan paytdagi original segmentlar sonini tiklaymiz
    # (source_indices yo'q - eski format - bo'lsa, blok = 1 ta segment deb hisoblanadi).
    old_translation_count = sum(len(b.get("source_indices") or [1]) for b in old_blocks)
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
    if not keys_manager.has_any_active_key(owner_id=video["owner_id"]):
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
    kid, raw_key = keys_manager.get_next_active_key(owner_id=video["owner_id"])
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

    invalidated_count = _invalidate_translation_blocks_covering(video_id, index)
    translation_cleared = invalidated_count > 0

    lang_note = f" (til: {storage_lang_label(effective_language)})" if language is not None else ""
    invalidate_note = (f" {invalidated_count} ta tarjima bloki tozalandi (shu segmentni qamrab olgan) "
                        "- qayta tarjima qiling." if translation_cleared else "")
    log(video_id, f"{index + 1}-segment Whisper orqali qayta olindi{lang_note}.{invalidate_note}")
    return {"text": new_text, "translation_cleared": translation_cleared, "invalidated_blocks": invalidated_count}


def _invalidate_translation_blocks_covering(video_id: str, source_index: int) -> int:
    """Berilgan ORIGINAL segment indeksini (source_index) qamrab olgan barcha
    yakuniy tarjima bloklarini topib, matnini tozalaydi (source_indices/start/end
    o'zgarmaydi - faqat matn qayta tarjima kutilayotganini bildiradi). Shu bloklarga
    mos, allaqachon 'completed' bo'lgan TTS segmentlari ham eskirgan hisoblanadi va
    'pending'ga qaytariladi (aks holda merge/render eskirgan audio bilan davom etadi).
    Nechta blok tozalanganini qaytaradi."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    translations = json.loads(video["translation_segments"] or "[]")
    if not translations:
        return 0
    changed = False
    invalidated_final_indices = []
    for i, block in enumerate(translations):
        src = block.get("source_indices")
        if src is None:
            src = [i]  # eski format - orqaga moslik
        if source_index in src and (block.get("text") or "").strip():
            block["text"] = ""
            changed = True
            invalidated_final_indices.append(i)
    if not changed:
        return 0
    plain = "\n\n".join((t.get("text") or "") for t in translations)
    _update_video(video_id, translation_text=plain,
                  translation_segments=json.dumps(translations, ensure_ascii=False))
    write_translation_results(video_id)

    if video["tts_job_id"] and invalidated_final_indices:
        # Matn ham bo'shatiladi (nafaqat status) - aks holda eski (endi mos
        # kelmaydigan) matn bilan qayta sintez qilinib ketishi mumkin edi.
        import tts
        tts.rebuild_job_units(video["tts_job_id"], translations, invalidated_final_indices)
    return len(invalidated_final_indices)


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


def get_translation_memory_context(owner_id: str) -> str:
    """Sozlamalarda saqlangan Instruksiya/Kontekst va xotiraga qo'shilgan barcha
    qoidalarni birlashtirib, tarjima so'roviga qo'shish uchun tayyorlaydi."""
    notes = db.fetchall("SELECT content FROM translation_memory_notes WHERE owner_id = ? ORDER BY created_at ASC",
                        (owner_id,))
    if not notes:
        return ""
    return "\n".join(f"- {n['content']}" for n in notes)


def _chunk_original_segments(segments: list, chunk_size: int = 100, pad_search: int = 20,
                              min_gap: float = 1.5):
    """Uzun videolar uchun segmentlarni LLM so'roviga mos guruhlarga bo'ladi (har
    bir chaqiruv uchun katta massivni yuborish javobni kesilishi/xatosiga olib
    kelishi mumkin). Chegara joylashgan segment atrofida (pad_search doirasida)
    tabiiy pauza (>= min_gap soniya) qidiriladi va chegara shu yerga moslashtiriladi
    - shu orqali bitta gapni ikki bo'lak orasida bo'lib yuborish ehtimoli kamayadi
    (garov emas, lekin kuchli evristika). Qaytaradi: [(offset, chunk_segments), ...]
    - offset shu chunkning birinchi elementi to'liq ro'yxatdagi (0-based) indeksi."""
    n = len(segments)
    if n <= chunk_size:
        return [(0, segments)]
    chunks = []
    start = 0
    while start < n:
        ideal_end = min(start + chunk_size, n)
        if ideal_end >= n:
            chunks.append((start, segments[start:n]))
            break
        best_end = ideal_end
        best_gap = -1.0
        lo = max(start + 1, ideal_end - pad_search)
        hi = min(n - 1, ideal_end + pad_search)
        for i in range(lo, hi + 1):
            gap = segments[i]["start"] - segments[i - 1]["end"]
            if gap >= min_gap and gap > best_gap:
                best_gap = gap
                best_end = i
        chunks.append((start, segments[start:best_end]))
        start = best_end
    return chunks


async def run_auto_translate(video_id: str, provider: str = "openai"):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    try:
        segments = json.loads(video["transcript_segments"] or "[]")
        if not segments:
            raise RuntimeError("Original matn segmentlari topilmadi.")
        kid, raw = keys_manager.get_next_active_key(provider=provider, owner_id=video["owner_id"])
        if not raw:
            provider_label = "Claude" if provider == "claude" else "OpenAI"
            raise RuntimeError(f"Ishlaydigan {provider_label} API kalit topilmadi. Avval API kalit qo'shing.")

        instruction = db.get_user_setting(video["owner_id"], "translation_instruction", "") or ""
        context = db.get_user_setting(video["owner_id"], "translation_context", "") or ""
        memory_notes = get_translation_memory_context(video["owner_id"])
        full_context = "\n\n".join(x for x in (context, memory_notes) if x)

        chunks = _chunk_original_segments(segments)
        all_blocks = []
        total_input_tok = total_output_tok = 0
        usage_present = False
        async with httpx.AsyncClient() as client:
            for offset, chunk_segments in chunks:
                if provider == "claude":
                    chunk_blocks, usage = await translation.translate_segments_via_claude(
                        client, raw, chunk_segments, extra_instructions=instruction, extra_context=full_context)
                else:
                    chunk_blocks, usage = await translation.translate_segments_via_openai(
                        client, raw, chunk_segments, extra_instructions=instruction, extra_context=full_context)
                for b in chunk_blocks:
                    all_blocks.append({
                        "source_indices": [offset + x for x in b["source_indices"]],
                        "start": b["start"], "end": b["end"], "text": b["text"],
                    })
                if usage:
                    usage_present = True
                    total_input_tok += usage.get("prompt_tokens", 0)
                    total_output_tok += usage.get("completion_tokens", 0)
        keys_manager.mark_result(kid, True)

        translation_segments = [{"final_index": i, **b} for i, b in enumerate(all_blocks)]
        plain = "\n\n".join(b["text"] for b in all_blocks)
        _update_video(video_id, translation_text=plain,
                      translation_segments=json.dumps(translation_segments, ensure_ascii=False),
                      translation_status="ready", translation_source=f"auto_{provider}", status="translation_ready",
                      blocked_reason=None, error=None, message="Avtomatik tarjima tayyor.")

        if usage_present:
            if provider == "claude":
                cost = round((total_input_tok / 1_000_000) * 1.0 + (total_output_tok / 1_000_000) * 5.0, 6)
            else:
                cost = round((total_input_tok / 1_000_000) * 0.15 + (total_output_tok / 1_000_000) * 0.60, 6)
        else:
            cost = translation.estimate_translation_cost(
                sum(len(s["text"]) for s in segments), sum(len(b["text"]) for b in all_blocks), provider=provider)
        provider_label = "Claude Haiku" if provider == "claude" else "OpenAI gpt-4o-mini"
        chunk_note = f", {len(chunks)} qismda yuborildi" if len(chunks) > 1 else ""
        db.add_cost(video_id, "translation", cost, detail=f"{provider_label} avtomatik tarjima{chunk_note}")
        write_translation_results(video_id)
        log(video_id, f"Avtomatik tarjima tayyor ({provider_label}{chunk_note}, "
                       f"{len(segments)} ta original segment -> {len(all_blocks)} ta yakuniy blok).")
    except Exception as e:
        _update_video(video_id, translation_status="failed", message=f"Tarjima xatosi: {e}")
        log(video_id, f"XATO (tarjima): {e}\n{traceback.format_exc()[-400:]}")


async def fill_empty_translations(video_id: str, provider: str = "openai"):
    """Faqat matni bo'sh qolgan YAKUNIY tarjima bloklarini AI orqali to'ldiradi -
    to'liq qayta tarjima qilmaydi, allaqachon mavjud bloklarga/chegaralarga
    tegmaydi. Har bir bo'sh blok allaqachon "bitta yakuniy gap" sifatida
    belgilangan bo'lgani uchun, uni qamrab olgan original segmentlar matni
    birlashtirilib BITTA pseudo-segment sifatida yuboriladi - shu orqali AI
    bloklarni qayta guruhlashga urinmaydi (urinsa - aniq xato bilan to'xtatiladi)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    try:
        originals = json.loads(video["transcript_segments"] or "[]")
        translations = json.loads(video["translation_segments"] or "[]")
        if not originals or not translations:
            raise RuntimeError("Tarjima segmentlari topilmadi.")
        empty_indices = [i for i, t in enumerate(translations) if not (t.get("text") or "").strip()]
        if not empty_indices:
            _update_video(video_id, blocked_reason=None, message="Bo'sh bo'lak topilmadi.")
            return
        kid, raw = keys_manager.get_next_active_key(provider=provider, owner_id=video["owner_id"])
        if not raw:
            provider_label = "Claude" if provider == "claude" else "OpenAI"
            raise RuntimeError(f"Ishlaydigan {provider_label} API kalit topilmadi. Avval API kalit qo'shing.")

        instruction = db.get_user_setting(video["owner_id"], "translation_instruction", "") or ""
        context = db.get_user_setting(video["owner_id"], "translation_context", "") or ""
        memory_notes = get_translation_memory_context(video["owner_id"])
        full_context = "\n\n".join(x for x in (context, memory_notes) if x)

        pseudo_segments = []
        for i in empty_indices:
            block = translations[i]
            src = block.get("source_indices") or [i]
            covered_text = " ".join(
                (originals[x]["text"] or "").strip() for x in src if 0 <= x < len(originals)
            ).strip()
            pseudo_segments.append({"start": block.get("start"), "end": block.get("end"), "text": covered_text})

        chunks = _chunk_original_segments(pseudo_segments)
        result_texts_by_index = {}
        total_input_tok = total_output_tok = 0
        usage_present = False
        async with httpx.AsyncClient() as client:
            for offset, chunk_segments in chunks:
                if provider == "claude":
                    chunk_blocks, usage = await translation.translate_segments_via_claude(
                        client, raw, chunk_segments, extra_instructions=instruction, extra_context=full_context)
                else:
                    chunk_blocks, usage = await translation.translate_segments_via_openai(
                        client, raw, chunk_segments, extra_instructions=instruction, extra_context=full_context)
                if len(chunk_blocks) != len(chunk_segments) or any(
                        len(b["source_indices"]) != 1 for b in chunk_blocks):
                    raise RuntimeError(
                        "Bo'sh bo'laklarni to'ldirishda AI ularni qayta guruhlashga urindi - har bir bo'sh "
                        "blok mustaqil to'ldirilishi kerak edi. Qayta urinib ko'ring."
                    )
                for b in chunk_blocks:
                    local_i = b["source_indices"][0]
                    result_texts_by_index[offset + local_i] = b["text"]
                if usage:
                    usage_present = True
                    total_input_tok += usage.get("prompt_tokens", 0)
                    total_output_tok += usage.get("completion_tokens", 0)
        keys_manager.mark_result(kid, True)

        for pseudo_i, orig_block_i in enumerate(empty_indices):
            translations[orig_block_i]["text"] = result_texts_by_index[pseudo_i]
        plain = "\n\n".join((t.get("text") or "") for t in translations)
        _update_video(video_id, translation_text=plain,
                      translation_segments=json.dumps(translations, ensure_ascii=False),
                      translation_status="ready", status="translation_ready",
                      blocked_reason=None, error=None,
                      message=f"{len(empty_indices)} ta bo'sh bo'lak avtomatik tarjima qilindi.")

        if usage_present:
            if provider == "claude":
                cost = round((total_input_tok / 1_000_000) * 1.0 + (total_output_tok / 1_000_000) * 5.0, 6)
            else:
                cost = round((total_input_tok / 1_000_000) * 0.15 + (total_output_tok / 1_000_000) * 0.60, 6)
        else:
            cost = translation.estimate_translation_cost(
                sum(len(s["text"]) for s in pseudo_segments),
                sum(len(t) for t in result_texts_by_index.values()), provider=provider)
        provider_label = "Claude Haiku" if provider == "claude" else "OpenAI gpt-4o-mini"
        db.add_cost(video_id, "translation", cost,
                    detail=f"{provider_label}: {len(empty_indices)} bo'sh bo'lak to'ldirildi")
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
    active = transcription.active_timeline_points(freeze_points)
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
    log(video_id, f"Yakuniy (video vaqtiga moslashtirilgan) o'zbekcha subtitr fayllar yaratildi "
                   f"({transcription.timeline_message(active)}; jami {transcription.total_timeline_extra(active):.2f}s siljish).")


def get_translation_blocks(video_id: str) -> list:
    """Har bir YAKUNIY tarjima blokini (bir yoki bir nechta original segmentni
    mexanik ravishda birlashtirgan bo'lishi mumkin), uning qamrab olgan original
    matni va audio holati bilan qaytaradi ('Tahrirlash va audio' bo'limi uchun).
    `source_indices` - shu blok qamrab olgan original segment(lar)ning 0-based
    indeksi(lari) (eski, source_indices'siz ma'lumot uchun [index] deb qaraladi -
    orqaga moslik)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    originals = json.loads(video["transcript_segments"] or "[]")
    translations = json.loads(video["translation_segments"] or "[]")
    import tts
    import tts_plan
    audio_status_by_index = tts.block_audio_status(video["tts_job_id"]) if video["tts_job_id"] else {}
    # Gap chegarasi (UI'da ingichka chiziq) - TTS shu gaplar bo'yicha yaratiladi.
    sentence_ids = tts_plan.sentence_ids_for_blocks(translations)
    blocks = []
    for i, block in enumerate(translations):
        src = block.get("source_indices")
        if not src:
            src = [i]  # eski format - orqaga moslik
        original_text = " ".join(
            (originals[x]["text"] or "").strip() for x in src if 0 <= x < len(originals)
        ).strip()
        blocks.append({
            "index": i, "source_indices": src,
            "start": block.get("start"), "end": block.get("end"),
            "original_text": original_text, "translation_text": block.get("text") or "",
            # Tashqi SRT'dan (parse_srt_direct) kelgan ixtiyoriy tezlik belgisi -
            # "fast"/"slow"/None (oddiy). Faqat ko'rsatish uchun, bu yerda o'zgartirilmaydi.
            "speed_tag": block.get("speed_tag"),
            "speaker": block.get("speaker"),
            "sentence": sentence_ids[i],
            "audio": audio_status_by_index.get(i),
        })
    return blocks


def apply_block_edits(video_id: str, new_texts: list):
    """Faqat o'zgargan YAKUNIY bloklarni qayta ishlash uchun belgilaydi -
    o'zgarmagan bloklarning tayyor audiosi saqlanib qoladi (§10-13). Har bir
    blokning source_indices/start/end o'zgarishsiz qoladi - faqat matni
    yangilanadi."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    translations = json.loads(video["translation_segments"] or "[]")
    if len(new_texts) != len(translations):
        raise ValueError(f"Bloklar soni mos kelmadi: {len(new_texts)} != {len(translations)}")

    changed_indices = []
    for i, block in enumerate(translations):
        new_text = new_texts[i]
        if (block.get("text") or "") != new_text:
            changed_indices.append(i)
        block["text"] = new_text

    plain = "\n\n".join((t.get("text") or "") for t in translations)
    _update_video(video_id, translation_text=plain,
                  translation_segments=json.dumps(translations, ensure_ascii=False))
    write_translation_results(video_id)
    log(video_id, f"Tarjima tahrirlandi: {len(changed_indices)} ta blok o'zgardi.")

    if not changed_indices:
        return {"changed_count": 0, "audio_requeued": False}

    if video["tts_job_id"]:
        import tts
        # Blok kirgan GAP qayta yaratiladi (gap - TTS birligi), qolganlari keshdan/tayyor.
        # Eski (blokma-blok yaratilgan) ishlarda faqat o'zgargan blok - eski audio saqlanadi.
        legacy = tts.is_legacy_job(video["tts_job_id"])
        regen = tts.rebuild_job_units(video["tts_job_id"], translations, changed_indices)
        unit = "blok" if legacy else "gap"
        db.execute("UPDATE tts_jobs SET status = 'queued', error = NULL WHERE id = ?", (video["tts_job_id"],))
        _update_video(video_id, status="audio_processing", blocked_reason=None, audio_status="generating",
                      message=f"{regen} ta {unit} uchun audio qayta yaratilmoqda...")
        tts.TTS_QUEUE.put_nowait(video["tts_job_id"])
        log(video_id, f"{len(changed_indices)} ta o'zgargan blok: {regen} ta {unit} qayta yaratiladi.")
        return {"changed_count": len(changed_indices), "audio_requeued": True,
                "regenerate_count": regen, "regenerate_unit": unit}

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


async def _mux_render_core(video: dict, audio_path: Path, freeze_points: list, out_path: Path,
                            tmp_out_path: Path, log_prefix: str = ""):
    """Umumiy render yadrosi: audio_path + freeze_points asosida video["path"]dan
    yakuniy videoni tmp_out_path'ga yig'adi, muvaffaqiyatli bo'lsa out_path'ga
    ATOMIK ko'chiradi (shuning uchun jarayon o'rtada xato bilan to'xtasa ham,
    avvalgi sog'lom yakuniy video hech qachon yarim buzilgan holatda
    qolmaydi/almashtirilmaydi). Ham asosiy render_video(), ham video
    'completed' bo'lgach qo'shimcha provayder bilan yaratiladigan
    render_track_video() shu bitta yadroni ishlatadi - ikkalasida ham bir xil
    xavfsizlik va freeze/-t mantig'i qo'llanadi."""
    video_id = video["id"]
    if not audio_path or not audio_path.exists():
        raise RuntimeError("Audio fayl topilmadi. Avval audio yarating.")
    audio_duration = transcription.get_duration_seconds(audio_path)
    if audio_duration < 1.0:
        raise RuntimeError(
            f"Audio fayl bo'sh yoki juda qisqa ({audio_duration:.2f}s). Audio faylni qayta yarating."
        )
    tmp_out_path.unlink(missing_ok=True)

    active_freeze_points = transcription.active_timeline_points(freeze_points)
    if active_freeze_points:
        log(video_id, f"{log_prefix}Video yig'ilmoqda: {transcription.timeline_message(active_freeze_points)}. "
                       f"Video qayta kodlanadi - uzun videoda ancha vaqt oladi.")
    else:
        log(video_id, f"{log_prefix}Video va audio ffmpeg orqali birlashtirilmoqda (fayl hajmiga qarab bir necha "
                       f"daqiqa vaqt olishi mumkin)...")
    freeze_work_dir = CHUNKS_DIR / video_id / f"freeze_work_{tmp_out_path.stem}"

    # Yakuniy fayl aniq shu davomiylikda chiqishi kerak: asl video davomiyligi +
    # freeze'lar yig'indisi. Bu "-shortest" o'rniga "-t" bilan ishlatiladi -
    # qisqaroq audio videoni kesib qo'ymaydi, va ortiqcha uzunlikdan ham himoya qiladi.
    video_duration = float(video["duration"] or 0) or transcription.get_duration_seconds(Path(video["path"]))
    target_duration = None
    if video_duration and video_duration > 0:
        target_duration = video_duration + transcription.total_freeze_duration(active_freeze_points)
        log(video_id, f"{log_prefix}DEBUG render: video_duration={video_duration:.3f}s "
                       f"extra_total={transcription.total_timeline_extra(active_freeze_points):.3f}s "
                       f"target_duration={target_duration:.3f}s")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, transcription.mux_video_audio_with_freezes,
        Path(video["path"]), audio_path, tmp_out_path, freeze_points, freeze_work_dir, target_duration)

    tmp_out_path.replace(out_path)  # atomik ko'chirish - endigina yakuniy video hisoblanadi


async def render_video(video_id: str):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    tmp_out_path = None
    try:
        out_dir = RESULTS_DIR / video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        base = safe_name(Path(video["original_name"]).stem) or "video"
        out_path = out_dir / f"{base}_yakuniy.mp4"
        tmp_out_path = out_dir / f"{base}_yakuniy.rendering.mp4"

        freeze_points = json.loads(video["freeze_points"]) if video["freeze_points"] else []
        audio_path = Path(video["audio_path"]) if video["audio_path"] else None
        await _mux_render_core(video, audio_path, freeze_points, out_path, tmp_out_path)

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
    if job["for_track"]:
        # Bu asosiy (primary) audio EMAS - video 'completed' bo'lgach ikkinchi
        # provayder bilan qo'shimcha yaratilgan track, YOKI "Ruscha o'rganish"
        # treki. Ikkalasida ham videos.* (asosiy) maydonlarga UMUMAN tegilmaydi.
        if job["is_learning"]:
            _sync_learning_track_from_job(job)
        else:
            _sync_audio_track_from_job(job)
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
        timeline_text = transcription.timeline_message(freeze_points)
        message = "Audio tayyor."
        if timeline_text:
            message = f"Audio tayyor. Yakuniy videoda: {timeline_text}."
        _update_video(video_id, status="audio_ready", blocked_reason=None, audio_status="ready",
                      audio_path=job["result_path"], freeze_points=job["freeze_points"], message=message)
        write_final_subtitles(video_id, freeze_points)
        log(video_id, f"Audio tayyor (TTS ishi yakunlandi).{' ' + timeline_text + '.' if timeline_text else ''}")

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
#     QO'SHIMCHA AUDIO/VIDEO TRACK (video 'completed' bo'lgach IKKINCHI
#     provayder bilan yaratiladi - asosiy natijaga UMUMAN tegmaydi)
# ---------------------------------------------------------------------------

PROVIDER_LABELS = {"aisha": "Aisha", "openai": "OpenAI"}


def write_track_final_subtitles(video_id: str, provider: str, freeze_points: list):
    """write_final_subtitles() bilan bir xil mantiq, lekin QO'SHIMCHA track
    uchun - natija fayllari alohida 'kind' bilan saqlanadi (masalan
    'vtt_uz_final_openai'), asosiy natijalarga tegilmaydi."""
    srt_kind = f"srt_uz_final_{provider}"
    vtt_kind = f"vtt_uz_final_{provider}"
    db.execute("DELETE FROM results WHERE video_id = ? AND kind IN (?, ?)", (video_id, srt_kind, vtt_kind))
    active = transcription.active_timeline_points(freeze_points)
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
    srt_path = out_dir / f"{base}.uz.final.{provider}.srt"
    vtt_path = out_dir / f"{base}.uz.final.{provider}.vtt"
    srt_path.write_text(srt_text, encoding="utf-8")
    vtt_path.write_text(vtt_text, encoding="utf-8")
    for kind, path in ((srt_kind, srt_path), (vtt_kind, vtt_path)):
        db.execute(
            "INSERT INTO results (id, video_id, kind, filename, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_id(), video_id, kind, path.name, str(path), db.now()))


def start_secondary_track(video_id: str, provider: str, voice: str = "", mood: str = "", speed: float = 1.0,
                           instructions: str = "", aisha_key: str = "", stretch_to_fit: bool = True) -> str:
    """Video 'completed' bo'lgach, IKKINCHI provayder bilan qo'shimcha
    audio+video yaratishni boshlaydi - asosiy (birinchi) natijaga UMUMAN
    tegmaydi (videos.* maydonlar o'zgarishsiz qoladi)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Video topilmadi.")
    if video["status"] != "completed":
        raise ValueError("Avval asosiy video tayyor bo'lishi kerak.")
    primary_job = (db.fetchone("SELECT provider FROM tts_jobs WHERE id = ?", (video["tts_job_id"],))
                   if video["tts_job_id"] else None)
    if primary_job and primary_job["provider"] == provider:
        raise ValueError("Bu provayder bilan asosiy audio allaqachon shu video uchun ishlatilgan.")
    existing_track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?",
                                  (video_id, provider))
    if existing_track and existing_track["audio_status"] == "generating":
        raise ValueError("Bu provayder uchun audio hozir allaqachon yaratilmoqda.")
    segments = json.loads(video["translation_segments"] or "[]")
    if not segments:
        raise ValueError("Tarjima segmentlari topilmadi.")

    import tts
    job_id = tts.create_job(video["original_name"], provider, segments, voice, mood, speed, instructions,
                             aisha_key, stretch_to_fit, video_id=video_id, for_track=True)
    now = db.now()
    if existing_track:
        db.execute("UPDATE audio_tracks SET tts_job_id = ?, audio_status = 'generating', audio_path = NULL, "
                   "final_video_path = NULL, final_video_status = 'none', freeze_points = NULL, error = NULL, "
                   "updated_at = ? WHERE id = ?", (job_id, now, existing_track["id"]))
    else:
        db.execute(
            "INSERT INTO audio_tracks (id, video_id, provider, tts_job_id, audio_status, final_video_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'generating', 'none', ?, ?)",
            (db.new_id(), video_id, provider, job_id, now, now))
    log(video_id, f"Qo'shimcha audio ({PROVIDER_LABELS.get(provider, provider)}) yaratish boshlandi.")
    return job_id


def _sync_audio_track_from_job(job: dict):
    """Qo'shimcha (video 'completed' bo'lgach ikkinchi provayder bilan
    yaratilgan) TTS ish holati o'zgarganda audio_tracks jadvalini yangilaydi -
    asosiy videos.* maydonlarga UMUMAN tegmaydi."""
    video_id = job["video_id"]
    track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?",
                         (video_id, job["provider"]))
    if not track or track["tts_job_id"] != job["id"]:
        log(video_id, f"Eski qo'shimcha audio ish ({job['id']}) tugadi, lekin track endi boshqa ishga "
                       f"bog'langan - e'tiborsiz qoldirildi.")
        return
    label = PROVIDER_LABELS.get(job["provider"], job["provider"])
    if job["status"] == "completed":
        freeze_points = []
        if job["freeze_points"]:
            try:
                freeze_points = json.loads(job["freeze_points"])
            except Exception:
                pass
        db.execute("UPDATE audio_tracks SET audio_status = 'ready', audio_path = ?, freeze_points = ?, "
                   "error = NULL, updated_at = ? WHERE id = ?",
                   (job["result_path"], job["freeze_points"], db.now(), track["id"]))
        write_track_final_subtitles(video_id, job["provider"], freeze_points)
        log(video_id, f"[{label}] Qo'shimcha audio tayyor.")
        full_video = db.fetchone("SELECT path FROM videos WHERE id = ?", (video_id,))
        if full_video and full_video["path"] and Path(full_video["path"]).exists() and job["result_path"]:
            enqueue_track_render(video_id, job["provider"])
        else:
            db.execute("UPDATE audio_tracks SET final_video_status = 'error', error = ?, updated_at = ? "
                       "WHERE id = ?", ("Original video fayli topilmadi.", db.now(), track["id"]))
    elif job["status"] == "paused_api_key":
        db.execute("UPDATE audio_tracks SET audio_status = 'error', error = ?, updated_at = ? WHERE id = ?",
                   ("Ishlaydigan OpenAI API kalit topilmadi.", db.now(), track["id"]))
        log(video_id, f"[{label}] Qo'shimcha audio: API kalit topilmadi.")
    elif job["status"] == "error":
        db.execute("UPDATE audio_tracks SET audio_status = 'error', error = ?, updated_at = ? WHERE id = ?",
                   (job["error"] or "", db.now(), track["id"]))
        log(video_id, f"[{label}] Qo'shimcha audio yaratishda xato: {job['error']}")
    elif job["status"] == "cancelled":
        db.execute("UPDATE audio_tracks SET audio_status = 'error', error = ?, updated_at = ? WHERE id = ?",
                   ("Bekor qilindi.", db.now(), track["id"]))


def enqueue_track_render(video_id: str, provider: str) -> bool:
    """Qo'shimcha (track) yakuniy videoni yig'ish navbatiga qo'yadi. ALLAQACHON
    shu track uchun render ketayotgan bo'lsa, qayta navbatga qo'ymaydi va
    False qaytaradi - parallel ikkita render'ning oldini olish uchun."""
    track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?", (video_id, provider))
    if not track:
        return False
    if track["final_video_status"] == "generating":
        return False
    db.execute("UPDATE audio_tracks SET final_video_status = 'generating', error = NULL, updated_at = ? "
               "WHERE id = ?", (db.now(), track["id"]))
    log(video_id, f"Qo'shimcha video ({PROVIDER_LABELS.get(provider, provider)}) yig'ish navbatga qo'yildi.")
    TRACK_RENDER_QUEUE.put_nowait((video_id, provider))
    return True


async def render_track_video(video_id: str, provider: str):
    track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?", (video_id, provider))
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not track or not video:
        return
    label = PROVIDER_LABELS.get(provider, provider)
    tmp_out_path = None
    try:
        out_dir = RESULTS_DIR / video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        base = safe_name(Path(video["original_name"]).stem) or "video"
        out_path = out_dir / f"{base}_yakuniy_{provider}.mp4"
        tmp_out_path = out_dir / f"{base}_yakuniy_{provider}.rendering.mp4"

        freeze_points = json.loads(track["freeze_points"]) if track["freeze_points"] else []
        audio_path = Path(track["audio_path"]) if track["audio_path"] else None
        await _mux_render_core(video, audio_path, freeze_points, out_path, tmp_out_path, log_prefix=f"[{label}] ")

        db.execute("UPDATE audio_tracks SET final_video_status = 'ready', final_video_path = ?, error = NULL, "
                   "updated_at = ? WHERE id = ?", (str(out_path), db.now(), track["id"]))
        log(video_id, f"[{label}] Qo'shimcha yakuniy video tayyor.")
    except Exception as e:
        if tmp_out_path:
            tmp_out_path.unlink(missing_ok=True)
        db.execute("UPDATE audio_tracks SET final_video_status = 'error', error = ?, updated_at = ? WHERE id = ?",
                   (str(e), db.now(), track["id"]))
        log(video_id, f"XATO ([{label}] qo'shimcha render): {e}\n{traceback.format_exc()[-400:]}")


async def track_render_consumer():
    while True:
        video_id, provider = await TRACK_RENDER_QUEUE.get()
        try:
            await render_track_video(video_id, provider)
        except Exception as e:
            log(video_id, f"XATO ([{provider}] track render consumer): {e}\n{traceback.format_exc()[-500:]}")
        finally:
            TRACK_RENDER_QUEUE.task_done()


# ---------------------------------------------------------------------------
#     "RUSCHA O'RGANISH" (LEARNING) TREKI - foydalanuvchi qo'lda yuklagan
#     tayyor Learning SRT asosida, MAVJUD TTS va render mexanizmi orqali
#     yaratiladigan MUSTAQIL audio/video. Asosiy Uzbek pipeline'ga (videos.*)
#     va audio_tracks jadvaliga UMUMAN tegmaydi - alohida learning_tracks
#     jadvali va alohida LEARNING_RENDER_QUEUE ishlatiladi. Dastur bu yerda
#     hech qanday SRT YARATMAYDI - faqat foydalanuvchi yuklagan faylni
#     o'zgartirmasdan saqlaydi va o'qiydi.
# ---------------------------------------------------------------------------

def cleanup_learning_track(video_id: str, delete_record: bool = False):
    """Learning trekining TTS job va hosila fayllarini xavfsiz tozalaydi.

    ``delete_record=False`` yangi SRT yuklashdan oldingi hosilalarni olib
    tashlaydi, lekin learning_tracks yozuvini saqlaydi. ``True`` esa video
    butunlay o'chirilganda/restart qilinganda jadval yozuvini ham o'chiradi.
    """
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track:
        return
    job_id = track["tts_job_id"]
    if job_id:
        from storage import TTS_DIR
        import tts as tts_module
        tts_module.PAUSE_FLAGS.pop(job_id, None)
        tts_module.CANCEL_FLAGS[job_id] = True
        db.execute("DELETE FROM tts_segments WHERE job_id = ?", (job_id,))
        db.execute("DELETE FROM tts_jobs WHERE id = ?", (job_id,))
        shutil.rmtree(TTS_DIR / job_id, ignore_errors=True)
    for key in ("audio_path", "final_video_path", "export_video_path", "intro_video_path",
                "subtitled_video_path"):
        if track[key]:
            Path(track[key]).unlink(missing_ok=True)
    out_dir = RESULTS_DIR / video_id
    shutil.rmtree(out_dir / "learning_intro_slides", ignore_errors=True)
    if out_dir.exists():
        for pattern in ("*.ru-learning.final.srt", "*.ru-learning.final.vtt",
                        "*_yakuniy_learning.rendering.mp4"):
            for path in out_dir.glob(pattern):
                path.unlink(missing_ok=True)
    db.execute("DELETE FROM results WHERE video_id = ? AND kind IN (?, ?)",
               (video_id, "srt_ru_learning_final", "vtt_ru_learning_final"))
    if delete_record:
        db.execute("DELETE FROM learning_tracks WHERE video_id = ?", (video_id,))


def apply_learning_srt(video_id: str, srt_text: str, filename: str, segment_count: int):
    """Foydalanuvchi yuklagan Learning SRT'ni xom holda diskka yozadi va
    learning_tracks jadvalini yangilaydi (UPSERT). Yangi SRT eski audio/video
    bilan endi mos emasligi uchun ulardan qolgan eski natijalarni tozalaydi -
    audio_tracks eski trekni tozalashdagi bilan bir xil mantiq."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Video topilmadi.")
    # So'z teglari: xato bo'lsa (LearningSrtError) yuklash rad etiladi - hech narsa o'zgarmaydi.
    blocks = translation.parse_learning_srt(srt_text)
    warnings = translation.learning_srt_warnings(blocks)
    existing = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if existing and existing["tts_job_id"]:
        old_job = db.fetchone("SELECT status FROM tts_jobs WHERE id = ?", (existing["tts_job_id"],))
        if old_job and old_job["status"] in ("queued", "running"):
            raise ValueError("Learning audio hozir yaratilmoqda. Yangi SRT yuklashdan oldin jarayon tugashini kuting.")
    if existing and "generating" in (existing["intro_status"], existing["export_status"]):
        raise ValueError("Learning intro/video hozir yaratilmoqda. Yangi SRT yuklashdan oldin jarayon tugashini kuting.")
    if existing:
        cleanup_learning_track(video_id, delete_record=False)

    out_dir = RESULTS_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(Path(video["original_name"]).stem) or "video"
    srt_path = out_dir / f"{base}.learning.srt"
    srt_path.write_text(srt_text, encoding="utf-8")

    now = db.now()
    words_json = json.dumps(blocks, ensure_ascii=False)
    warnings_json = json.dumps(warnings, ensure_ascii=False)
    existing = db.fetchone("SELECT id FROM learning_tracks WHERE video_id = ?", (video_id,))
    if existing:
        db.execute(
            "UPDATE learning_tracks SET srt_filename = ?, srt_path = ?, srt_status = 'uploaded', "
            "segment_count = ?, tts_job_id = NULL, audio_path = NULL, audio_status = 'none', "
            "freeze_points = NULL, final_video_path = NULL, final_video_status = 'none', error = NULL, "
            "words_json = ?, warnings_json = ?, export_status = 'none', export_video_path = NULL, "
            "export_with_intro = 0, export_error = NULL, intro_status = 'none', intro_progress = 0, "
            "intro_message = NULL, intro_error = NULL, intro_duration = 0, intro_video_path = NULL, "
            "intro_slides_json = NULL, subtitled_video_status = 'none', subtitled_video_path = NULL, "
            "subtitled_video_error = NULL, updated_at = ? WHERE id = ?",
            (filename, str(srt_path), segment_count, words_json, warnings_json, now, existing["id"]))
    else:
        db.execute(
            "INSERT INTO learning_tracks (id, video_id, srt_filename, srt_path, srt_status, segment_count, "
            "audio_status, final_video_status, words_json, warnings_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'uploaded', ?, 'none', 'none', ?, ?, ?, ?)",
            (db.new_id(), video_id, filename, str(srt_path), segment_count, words_json, warnings_json, now, now))
    lists = translation.learning_word_lists(blocks)
    log(video_id, f"Learning SRT yuklandi: {filename} ({segment_count} ta bo'lak, {len(lists['new'])} ta yangi, "
                  f"{len(lists['repeat'])} ta takror so'z, {len(warnings)} ta ogohlantirish).")


def start_learning_track(video_id: str, provider: str, voice: str = "", mood: str = "", speed: float = 1.0,
                          instructions: str = "", aisha_key: str = "", stretch_to_fit: bool = True) -> str:
    """Yuklangan Learning SRT asosida, MAVJUD TTS mexanizmi orqali (tts.create_job)
    mustaqil Learning audio yaratishni boshlaydi. Original video 'completed'
    bo'lishi SHART EMAS - ikki yo'nalish (Uzbek/Learning) mustaqil ishlaydi."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        raise ValueError("Video topilmadi.")
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["srt_status"] != "uploaded" or not track["srt_path"]:
        raise ValueError("Avval Learning SRT faylni yuklang.")
    if track["audio_status"] == "generating":
        raise ValueError("Learning audio hozir allaqachon yaratilmoqda.")
    if not video["path"] or not Path(video["path"]).exists():
        raise ValueError("Original video fayli topilmadi.")
    srt_file = Path(track["srt_path"])
    if not srt_file.exists():
        raise ValueError("Learning SRT fayli topilmadi. Qayta yuklang.")
    segments = translation.parse_srt_direct(srt_file.read_text(encoding="utf-8"))

    import tts
    job_id = tts.create_job(video["original_name"] + " (Ruscha o'rganish)", provider, segments, voice, mood,
                             speed, instructions, aisha_key, stretch_to_fit, video_id=video_id, for_track=True)
    db.execute("UPDATE tts_jobs SET is_learning = 1 WHERE id = ?", (job_id,))
    now = db.now()
    db.execute(
        "UPDATE learning_tracks SET provider = ?, voice = ?, mood = ?, speed = ?, instructions = ?, "
        "stretch_to_fit = ?, tts_job_id = ?, audio_status = 'generating', audio_path = NULL, "
        "final_video_path = NULL, final_video_status = 'none', freeze_points = NULL, error = NULL, "
        "export_status = 'none', export_error = NULL, subtitled_video_status = 'none', "
        "subtitled_video_path = NULL, subtitled_video_error = NULL, updated_at = ? WHERE video_id = ?",
        (provider, voice, mood, speed, instructions, 1 if stretch_to_fit else 0, job_id, now, video_id))
    log(video_id, f"Learning audio ({PROVIDER_LABELS.get(provider, provider)}) yaratish boshlandi.")
    return job_id


def write_learning_final_subtitles(video_id: str, freeze_points: list):
    """write_track_final_subtitles() bilan bir xil mantiq, lekin manba matnni
    video["translation_segments"]dan EMAS, foydalanuvchi yuklagan Learning
    SRT'dan (har safar qayta o'qib, translation.parse_srt_direct bilan) oladi -
    bitta manba-haqiqat (srt_path fayli), qo'shimcha sinxronizatsiya shart emas."""
    srt_kind, vtt_kind = "srt_ru_learning_final", "vtt_ru_learning_final"
    db.execute("DELETE FROM results WHERE video_id = ? AND kind IN (?, ?)", (video_id, srt_kind, vtt_kind))
    active = transcription.active_timeline_points(freeze_points)
    if not active:
        return
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or not track["srt_path"] or not Path(track["srt_path"]).exists():
        return
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    segments = translation.parse_srt_direct(Path(track["srt_path"]).read_text(encoding="utf-8"))
    adjusted = transcription.apply_freeze_to_segments(segments, active)
    srt_text = transcription.build_srt(adjusted)
    vtt_text = transcription.build_vtt(adjusted)
    out_dir = RESULTS_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(Path(video["original_name"]).stem) or "natija"
    srt_path = out_dir / f"{base}.ru-learning.final.srt"
    vtt_path = out_dir / f"{base}.ru-learning.final.vtt"
    srt_path.write_text(srt_text, encoding="utf-8")
    vtt_path.write_text(vtt_text, encoding="utf-8")
    for kind, path in ((srt_kind, srt_path), (vtt_kind, vtt_path)):
        db.execute(
            "INSERT INTO results (id, video_id, kind, filename, path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_id(), video_id, kind, path.name, str(path), db.now()))


def _sync_learning_track_from_job(job: dict):
    """_sync_audio_track_from_job() bilan bir xil naqsh, farqi: audio_tracks
    o'rniga learning_tracks (video_id bo'yicha, provider shart emas)."""
    video_id = job["video_id"]
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["tts_job_id"] != job["id"]:
        log(video_id, f"Eski Learning audio ish ({job['id']}) tugadi, lekin track endi boshqa ishga "
                       f"bog'langan - e'tiborsiz qoldirildi.")
        return
    if job["status"] == "completed":
        freeze_points = []
        if job["freeze_points"]:
            try:
                freeze_points = json.loads(job["freeze_points"])
            except Exception:
                pass
        db.execute("UPDATE learning_tracks SET audio_status = 'ready', audio_path = ?, freeze_points = ?, "
                   "error = NULL, updated_at = ? WHERE id = ?",
                   (job["result_path"], job["freeze_points"], db.now(), track["id"]))
        write_learning_final_subtitles(video_id, freeze_points)
        log(video_id, "Learning audio tayyor.")
        full_video = db.fetchone("SELECT path FROM videos WHERE id = ?", (video_id,))
        if full_video and full_video["path"] and Path(full_video["path"]).exists() and job["result_path"]:
            enqueue_learning_render(video_id)
        else:
            db.execute("UPDATE learning_tracks SET final_video_status = 'error', error = ?, updated_at = ? "
                       "WHERE id = ?", ("Original video fayli topilmadi.", db.now(), track["id"]))
    elif job["status"] == "paused_api_key":
        db.execute("UPDATE learning_tracks SET audio_status = 'error', error = ?, updated_at = ? WHERE id = ?",
                   ("Ishlaydigan OpenAI API kalit topilmadi.", db.now(), track["id"]))
        log(video_id, "Learning audio: API kalit topilmadi.")
    elif job["status"] == "error":
        db.execute("UPDATE learning_tracks SET audio_status = 'error', error = ?, updated_at = ? WHERE id = ?",
                   (job["error"] or "", db.now(), track["id"]))
        log(video_id, f"Learning audio yaratishda xato: {job['error']}")
    elif job["status"] == "cancelled":
        db.execute("UPDATE learning_tracks SET audio_status = 'error', error = ?, updated_at = ? WHERE id = ?",
                   ("Bekor qilindi.", db.now(), track["id"]))


def enqueue_learning_render(video_id: str) -> bool:
    """Learning yakuniy videoni yig'ish navbatiga qo'yadi. ALLAQACHON render
    ketayotgan bo'lsa qayta navbatga qo'ymaydi va False qaytaradi."""
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track:
        return False
    if track["final_video_status"] == "generating":
        return False
    db.execute("UPDATE learning_tracks SET final_video_status = 'generating', error = NULL, updated_at = ? "
               "WHERE id = ?", (db.now(), track["id"]))
    log(video_id, "Learning video yig'ish navbatga qo'yildi.")
    LEARNING_RENDER_QUEUE.put_nowait(video_id)
    return True


async def render_learning_video(video_id: str):
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not track or not video:
        return
    tmp_out_path = None
    try:
        out_dir = RESULTS_DIR / video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        base = safe_name(Path(video["original_name"]).stem) or "video"
        out_path = out_dir / f"{base}_yakuniy_learning.mp4"
        tmp_out_path = out_dir / f"{base}_yakuniy_learning.rendering.mp4"

        freeze_points = json.loads(track["freeze_points"]) if track["freeze_points"] else []
        audio_path = Path(track["audio_path"]) if track["audio_path"] else None
        await _mux_render_core(video, audio_path, freeze_points, out_path, tmp_out_path,
                                log_prefix="[Ruscha o'rganish] ")

        db.execute("UPDATE learning_tracks SET final_video_status = 'ready', final_video_path = ?, error = NULL, "
                   "updated_at = ? WHERE id = ?", (str(out_path), db.now(), track["id"]))
        log(video_id, "Russian Learning yakuniy video tayyor.")
        # Intro foydalanuvchidan alohida Aisha kaliti yoki qo'shimcha bosishni
        # talab qilmaydi. Teglar bo'lsa OpenAI orqali avtomatik yaratiladi;
        # intro tugagach run_learning_intro eksportni o'zi navbatga qo'yadi.
        lists = translation.learning_word_lists(learning_blocks(track))
        if lists["new"] or lists["repeat"]:
            enqueue_learning_intro(video_id)
        else:
            enqueue_learning_export(video_id)
    except Exception as e:
        if tmp_out_path:
            tmp_out_path.unlink(missing_ok=True)
        db.execute("UPDATE learning_tracks SET final_video_status = 'error', error = ?, updated_at = ? "
                   "WHERE id = ?", (str(e), db.now(), track["id"]))
        log(video_id, f"XATO (Learning render): {e}\n{traceback.format_exc()[-400:]}")


async def learning_render_consumer():
    while True:
        video_id = await LEARNING_RENDER_QUEUE.get()
        try:
            await render_learning_video(video_id)
        except Exception as e:
            log(video_id, f"XATO (Learning render consumer): {e}\n{traceback.format_exc()[-500:]}")
        finally:
            LEARNING_RENDER_QUEUE.task_done()


# ---------------------------------------------------------------------------
#     Learning: so'zlar kuydirilgan eksport (ASOS_learning.mp4) va intro
#     (ASOS_intro.mp4). Toza, intro'siz Learning videosi (final_video_path)
#     o'zgarmaydi - pleyer shuni "So'zlar" VTT treki bilan ko'rsatadi.
# ---------------------------------------------------------------------------

def learning_blocks(track: dict) -> list:
    try:
        return json.loads(track["words_json"]) if track and track["words_json"] else []
    except ValueError:
        return []


def learning_freeze_points(track: dict) -> list:
    try:
        return json.loads(track["freeze_points"]) if track and track["freeze_points"] else []
    except ValueError:
        return []


def learning_intro_offset(track: dict) -> float:
    if track and track["intro_status"] == "ready" and track["intro_video_path"] \
            and Path(track["intro_video_path"]).exists():
        return float(track["intro_duration"] or 0)
    return 0.0


def enqueue_learning_export(video_id: str) -> bool:
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["final_video_status"] != "ready":
        return False
    db.execute("UPDATE learning_tracks SET export_status = 'generating', export_error = NULL, updated_at = ? "
               "WHERE id = ?", (db.now(), track["id"]))
    LEARNING_EXTRA_QUEUE.put_nowait(("export", video_id))
    return True


def enqueue_learning_intro(video_id: str, strip_stress: bool = False) -> bool:
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    if not track or track["intro_status"] == "generating":
        return False
    fields = ["intro_status = 'generating'", "intro_progress = 0", "intro_message = ?", "intro_error = NULL",
              "intro_strip_stress = ?", "updated_at = ?"]
    params = ["Navbatda", 1 if strip_stress else 0, db.now()]
    db.execute(f"UPDATE learning_tracks SET {', '.join(fields)} WHERE id = ?", params + [track["id"]])
    log(video_id, "Learning intro yaratish navbatga qo'yildi.")
    LEARNING_EXTRA_QUEUE.put_nowait(("intro", video_id))
    return True


async def run_learning_export(video_id: str):
    import learning
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not track or not video or track["export_status"] != "generating":
        return
    try:
        clean = Path(track["final_video_path"] or "")
        if track["final_video_status"] != "ready" or not clean.exists():
            raise RuntimeError("Avval Learning videosi tayyor bo'lishi kerak.")
        offset = learning_intro_offset(track)
        intro_path = Path(track["intro_video_path"]) if offset > 0 else None
        cues = learning.words_cues(learning_blocks(track), learning_freeze_points(track), offset)
        burn = bool(cues) and learning.ass_filter_available()
        if not burn and intro_path is None:
            reason = ("Learning SRT'da so'z teglari yo'q." if not cues else
                      "ffmpeg build'ida libass (ass filtri) yo'q - so'zlar faqat pleyerdagi \"So'zlar\" treki "
                      "orqali ko'rinadi.")
            db.execute("UPDATE learning_tracks SET export_status = 'skipped', export_video_path = NULL, "
                       "export_with_intro = 0, export_error = ?, updated_at = ? WHERE id = ?",
                       (reason, db.now(), track["id"]))
            log(video_id, f"[Ruscha o'rganish] Eksport kerak emas: {reason}")
            return
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, learning.probe_media, clean)
        ass_text = learning.build_words_ass(cues, info["width"], info["height"]) if burn else None
        out_dir = RESULTS_DIR / video_id
        base = safe_name(Path(video["original_name"]).stem) or "video"
        out_path = out_dir / f"{base}_learning_export.mp4"
        tmp_path = out_dir / f"{base}_learning_export.rendering.mp4"
        work_dir = CHUNKS_DIR / video_id / "learning_export_work"
        log(video_id, "[Ruscha o'rganish] Yuklab olinadigan Learning videosi yig'ilmoqda"
                      + (" (intro bilan)" if intro_path else "") + "...")
        method = await loop.run_in_executor(None, learning.build_export, clean, tmp_path, work_dir, info,
                                            ass_text, intro_path)
        tmp_path.replace(out_path)
        shutil.rmtree(work_dir, ignore_errors=True)
        warn = None if burn or not cues else ("ffmpeg build'ida libass yo'q - so'zlar kadrga yozilmadi, "
                                              "faqat pleyer treki orqali ko'rinadi.")
        db.execute("UPDATE learning_tracks SET export_status = 'ready', export_video_path = ?, "
                   "export_with_intro = ?, export_error = ?, updated_at = ? WHERE id = ?",
                   (str(out_path), 1 if intro_path else 0, warn, db.now(), track["id"]))
        log(video_id, f"[Ruscha o'rganish] Yuklab olinadigan Learning videosi tayyor: {method}.")
    except Exception as e:
        db.execute("UPDATE learning_tracks SET export_status = 'error', export_error = ?, updated_at = ? "
                   "WHERE id = ?", (str(e)[:1500], db.now(), track["id"]))
        log(video_id, f"XATO (Learning eksport): {e}\n{traceback.format_exc()[-400:]}")


async def run_learning_intro(video_id: str):
    track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not track or not video or track["intro_status"] != "generating":
        return

    def progress(pct: float, message: str):
        db.execute("UPDATE learning_tracks SET intro_progress = ?, intro_message = ?, updated_at = ? "
                   "WHERE id = ?", (round(pct, 1), message, db.now(), track["id"]))

    try:
        # Importni try ichida saqlaymiz: Pillow kabi intro bog'liqligi
        # o'rnatilmagan bo'lsa vazifa abadiy "Navbatda" qolmasdan aniq
        # xato holatiga o'tishi kerak.
        import intro
        clean = Path(track["final_video_path"] or "")
        if track["final_video_status"] != "ready":
            raise RuntimeError("Intro Learning videosining parametrlari bilan yaratiladi - avval Learning "
                               "videosi tayyor bo'lishi kerak.")
        if not clean.exists():
            raise RuntimeError(f"Learning video fayli topilmadi: {clean}")
        voice = track["voice"] if track["provider"] == "openai" and track["voice"] else ""
        out_dir = RESULTS_DIR / video_id
        base = safe_name(Path(video["original_name"]).stem) or "video"
        out_path = out_dir / f"{base}_learning_intro.mp4"
        log(video_id, "[Ruscha o'rganish] Intro yaratilmoqda...")
        result = await intro.create_intro(
            learning_blocks(track), clean, out_path, out_dir / "learning_intro_slides",
            CHUNKS_DIR / video_id / "learning_intro_work", video_id, video["owner_id"], voice,
            bool(track["intro_strip_stress"]), progress)
        db.execute("UPDATE learning_tracks SET intro_status = 'ready', intro_progress = 100, intro_message = NULL, "
                   "intro_error = NULL, intro_duration = ?, intro_video_path = ?, intro_slides_json = ?, "
                   "updated_at = ? WHERE id = ?",
                   (result["duration"], str(out_path), result["slides_json"], db.now(), track["id"]))
        log(video_id, f"[Ruscha o'rganish] Intro tayyor: {result['duration']:.2f}s, "
                      f"{len(result['slides'])} ta ekran.")
        enqueue_learning_export(video_id)
    except Exception as e:
        db.execute("UPDATE learning_tracks SET intro_status = 'error', intro_error = ?, intro_message = NULL, "
                   "updated_at = ? WHERE id = ?", (str(e)[:1500], db.now(), track["id"]))
        log(video_id, f"XATO (Learning intro): {e}\n{traceback.format_exc()[-400:]}")


async def learning_extra_consumer():
    while True:
        kind, video_id = await LEARNING_EXTRA_QUEUE.get()
        try:
            if kind == "intro":
                await run_learning_intro(video_id)
            else:
                await run_learning_export(video_id)
        except Exception as e:
            log(video_id, f"XATO (Learning {kind} consumer): {e}\n{traceback.format_exc()[-500:]}")
        finally:
            LEARNING_EXTRA_QUEUE.task_done()


# ---------------------------------------------------------------------------
#     SUBTITR "KUYDIRISH" (HARDSUB) - ixtiyoriy, video 'completed' bo'lgach
#     (asosiy yoki qo'shimcha provayder treki uchun) foydalanuvchi so'rovi
#     bilan yaratiladi. Asosiy/qo'shimcha final_video_path'larga UMUMAN
#     tegmaydi - YANGI, alohida fayl yaratiladi. Shu sababli ESKI (avval
#     yaratilgan) videolar uchun ham, YANGI videolar uchun ham bir xil
#     ishlaydi - faqat 'completed' va yakuniy video tayyor bo'lishi shart.
# ---------------------------------------------------------------------------

def _pick_final_srt_path(video_id: str, provider: str = None):
    """Berilgan video (yoki uning provider treki)ning YAKUNIY (render qilingan
    videoning haqiqiy vaqt chizig'iga mos) o'zbekcha SRT faylini tanlaydi:
    avval freeze-moslashtirilgan 'final' variantni (freeze nuqtalari bo'lgan
    bo'lsa), topilmasa oddiy (source vaqtli) variantni - freeze bo'lmagan
    bo'lsa ular baribir bir xil vaqt chizig'ida bo'ladi."""
    final_kind = f"srt_uz_final_{provider}" if provider else "srt_uz_final"
    row = db.fetchone(
        "SELECT path FROM results WHERE video_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 1",
        (video_id, final_kind))
    if not row:
        row = db.fetchone(
            "SELECT path FROM results WHERE video_id = ? AND kind = 'srt_uz' ORDER BY created_at DESC LIMIT 1",
            (video_id,))
    return row["path"] if row else None


def _learning_burn_inputs(video_id: str, track: dict, video: dict):
    """Learning eksportiga mos video va SRT'ni qaytaradi.

    Intro tayyor bo'lsa eksport video intro bilan boshlanadi, shuning uchun
    subtitr avval freeze-pointlar, keyin intro uzunligiga suriladi. Eksport
    hali yo'q bo'lsa toza Learning video va faqat freeze-point ishlatiladi.
    """
    import learning

    use_export = (track["export_status"] == "ready" and track["export_video_path"]
                  and Path(track["export_video_path"]).exists())
    source_path = Path(track["export_video_path"] if use_export else track["final_video_path"])
    source_srt = Path(track["srt_path"] or "")
    if not source_srt.exists():
        return source_path, None
    blocks = translation.parse_srt_direct(source_srt.read_text(encoding="utf-8"))
    try:
        freeze_points = json.loads(track["freeze_points"]) if track["freeze_points"] else []
    except ValueError:
        freeze_points = []
    offset = float(track["intro_duration"] or 0) if use_export and track["export_with_intro"] else 0.0
    adjusted = learning.shifted_segments(blocks, freeze_points, offset)
    out_dir = RESULTS_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    base = safe_name(Path(video["original_name"]).stem) or "video"
    srt_path = out_dir / f"{base}.ru-learning.burn.srt"
    srt_path.write_text(transcription.build_srt(adjusted), encoding="utf-8")
    return source_path, srt_path


def enqueue_subtitle_burn(video_id: str, provider: str = None) -> bool:
    """Subtitr kuydirishni navbatga qo'yadi. Allaqachon 'generating' bo'lsa
    yoki manba video hali tayyor bo'lmasa - qayta qo'ymaydi (idempotent)."""
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return False
    if provider == "learning":
        track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            return False
        has_words = any(b.get("words") for b in learning_blocks(track))
        export_ready = (track["export_status"] == "ready" and track["export_video_path"]
                        and Path(track["export_video_path"]).exists())
        if has_words and not export_ready:
            return False
        if track["subtitled_video_status"] == "generating":
            return False
        db.execute("UPDATE learning_tracks SET subtitled_video_status = 'generating', "
                   "subtitled_video_error = NULL, updated_at = ? WHERE id = ?", (db.now(), track["id"]))
    elif provider:
        if video["status"] != "completed":
            return False
        track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?", (video_id, provider))
        if not track or track["final_video_status"] != "ready" or not track["final_video_path"]:
            return False
        if track["subtitled_video_status"] == "generating":
            return False
        db.execute("UPDATE audio_tracks SET subtitled_video_status = 'generating', subtitled_video_error = NULL, "
                   "updated_at = ? WHERE id = ?", (db.now(), track["id"]))
    else:
        if video["status"] != "completed":
            return False
        if video["final_video_status"] != "ready" or not video["final_video_path"]:
            return False
        if video["subtitled_video_status"] == "generating":
            return False
        _update_video(video_id, subtitled_video_status="generating", subtitled_video_error=None)
    label = f" ([{PROVIDER_LABELS.get(provider, provider)}])" if provider else ""
    log(video_id, f"Subtitrli video{label} yaratish navbatga qo'yildi.")
    SUBTITLE_BURN_QUEUE.put_nowait((video_id, provider))
    return True


async def burn_subtitles_job(video_id: str, provider: str = None):
    video = db.fetchone("SELECT * FROM videos WHERE id = ?", (video_id,))
    if not video:
        return
    track = None
    learning_track = None
    if provider == "learning":
        learning_track = db.fetchone("SELECT * FROM learning_tracks WHERE video_id = ?", (video_id,))
        if not learning_track:
            return
    elif provider:
        track = db.fetchone("SELECT * FROM audio_tracks WHERE video_id = ? AND provider = ?", (video_id, provider))
        if not track:
            return
    label = "[Ruscha o'rganish] " if provider == "learning" else \
        (f"[{PROVIDER_LABELS.get(provider, provider)}] " if provider else "")
    if learning_track:
        source_path, learning_srt_path = _learning_burn_inputs(video_id, learning_track, video)
        srt_path_str = str(learning_srt_path) if learning_srt_path else None
    else:
        source_path = Path(track["final_video_path"]) if track else Path(video["final_video_path"])
        srt_path_str = _pick_final_srt_path(video_id, provider)

    def _fail(msg: str):
        if learning_track:
            db.execute("UPDATE learning_tracks SET subtitled_video_status = 'error', subtitled_video_error = ?, "
                       "updated_at = ? WHERE id = ?", (msg, db.now(), learning_track["id"]))
        elif track:
            db.execute("UPDATE audio_tracks SET subtitled_video_status = 'error', subtitled_video_error = ?, "
                       "updated_at = ? WHERE id = ?", (msg, db.now(), track["id"]))
        else:
            _update_video(video_id, subtitled_video_status="error", subtitled_video_error=msg)
        log(video_id, f"XATO ({label}subtitr kuydirish): {msg}")

    if not srt_path_str or not Path(srt_path_str).exists():
        _fail("O'zbekcha subtitr (SRT) fayli topilmadi - avval yakuniy video tayyor bo'lishi kerak.")
        return
    if not source_path.exists():
        _fail("Manba (subtitrsiz) yakuniy video fayli topilmadi.")
        return

    tmp_out_path = None
    try:
        out_dir = RESULTS_DIR / video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        base = safe_name(Path(video["original_name"]).stem) or "video"
        suffix = f"_{provider}" if provider else ""
        out_path = out_dir / f"{base}_yakuniy_subtitrli{suffix}.mp4"
        tmp_out_path = out_dir / f"{base}_yakuniy_subtitrli{suffix}.rendering.mp4"

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, transcription.burn_subtitles_into_video, source_path, Path(srt_path_str), tmp_out_path)
        tmp_out_path.replace(out_path)

        if learning_track:
            db.execute("UPDATE learning_tracks SET subtitled_video_status = 'ready', subtitled_video_path = ?, "
                       "subtitled_video_error = NULL, updated_at = ? WHERE id = ?",
                       (str(out_path), db.now(), learning_track["id"]))
        elif track:
            db.execute("UPDATE audio_tracks SET subtitled_video_status = 'ready', subtitled_video_path = ?, "
                       "subtitled_video_error = NULL, updated_at = ? WHERE id = ?",
                       (str(out_path), db.now(), track["id"]))
        else:
            _update_video(video_id, subtitled_video_status="ready", subtitled_video_path=str(out_path),
                           subtitled_video_error=None)
        log(video_id, f"{label}Subtitrli video tayyor.")
    except Exception as e:
        if tmp_out_path:
            tmp_out_path.unlink(missing_ok=True)
        _fail(str(e))


async def subtitle_burn_consumer():
    while True:
        video_id, provider = await SUBTITLE_BURN_QUEUE.get()
        try:
            await burn_subtitles_job(video_id, provider)
        except Exception as e:
            log(video_id, f"XATO (subtitle burn consumer): {e}\n{traceback.format_exc()[-500:]}")
        finally:
            SUBTITLE_BURN_QUEUE.task_done()


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

    interrupted_track_render = db.fetchall(
        "SELECT video_id, provider FROM audio_tracks WHERE final_video_status = 'generating'")
    for t in interrupted_track_render:
        log(t["video_id"], f"Server qayta ishga tushdi - [{t['provider']}] qo'shimcha video yig'ish qayta boshlanadi.")
        TRACK_RENDER_QUEUE.put_nowait((t["video_id"], t["provider"]))

    interrupted_learning_render = db.fetchall(
        "SELECT video_id FROM learning_tracks WHERE final_video_status = 'generating'")
    for t in interrupted_learning_render:
        log(t["video_id"], "Server qayta ishga tushdi - Learning video yig'ish qayta boshlanadi.")
        LEARNING_RENDER_QUEUE.put_nowait(t["video_id"])

    for t in db.fetchall("SELECT video_id FROM learning_tracks WHERE intro_status = 'generating'"):
        log(t["video_id"], "Server qayta ishga tushdi - Learning intro yaratish davom ettiriladi.")
        LEARNING_EXTRA_QUEUE.put_nowait(("intro", t["video_id"]))
    for t in db.fetchall("SELECT video_id FROM learning_tracks WHERE export_status = 'generating'"):
        log(t["video_id"], "Server qayta ishga tushdi - Learning eksport qayta boshlanadi.")
        LEARNING_EXTRA_QUEUE.put_nowait(("export", t["video_id"]))

    # Yangi avtomatik intro funksiyasi deploy qilinishidan oldin tayyor bo'lgan
    # Learning videolarini ham bir marta avtomatik davom ettiradi. Xatoga tushgan
    # track qayta-qayta urinmaydi: uni UI'dagi "Qayta boshlash" boshqaradi.
    pending_auto_intro = db.fetchall(
        "SELECT video_id, words_json FROM learning_tracks "
        "WHERE final_video_status = 'ready' AND intro_status = 'none' "
        "AND export_status != 'generating'")
    for t in pending_auto_intro:
        try:
            lists = translation.learning_word_lists(json.loads(t["words_json"] or "[]"))
        except (TypeError, ValueError):
            lists = {"new": [], "repeat": []}
        if lists["new"] or lists["repeat"]:
            enqueue_learning_intro(t["video_id"])

    interrupted_subtitle_burn = db.fetchall(
        "SELECT id FROM videos WHERE subtitled_video_status = 'generating'")
    for v in interrupted_subtitle_burn:
        log(v["id"], "Server qayta ishga tushdi - subtitrli video yaratish qayta boshlanadi.")
        SUBTITLE_BURN_QUEUE.put_nowait((v["id"], None))
    interrupted_track_subtitle_burn = db.fetchall(
        "SELECT video_id, provider FROM audio_tracks WHERE subtitled_video_status = 'generating'")
    for t in interrupted_track_subtitle_burn:
        log(t["video_id"], f"Server qayta ishga tushdi - [{t['provider']}] subtitrli video yaratish qayta boshlanadi.")
        SUBTITLE_BURN_QUEUE.put_nowait((t["video_id"], t["provider"]))
    for t in db.fetchall("SELECT video_id FROM learning_tracks WHERE subtitled_video_status = 'generating'"):
        log(t["video_id"], "Server qayta ishga tushdi - Learning subtitrli video yaratish qayta boshlanadi.")
        SUBTITLE_BURN_QUEUE.put_nowait((t["video_id"], "learning"))

    for _ in range(MAX_ACTIVE_VIDEO_JOBS):
        asyncio.create_task(segment_consumer())
        asyncio.create_task(transcribe_consumer())
        asyncio.create_task(render_consumer())
        asyncio.create_task(track_render_consumer())
        asyncio.create_task(learning_render_consumer())
        asyncio.create_task(subtitle_burn_consumer())
    asyncio.create_task(learning_extra_consumer())

    import tts
    await tts.recover_and_start()
