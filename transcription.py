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


def _ffprobe_exe():
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    sibling = Path(ffmpeg_exe()).with_name("ffprobe")
    return str(sibling) if sibling.exists() else None


def _plan_keyframe_cuts(input_path: Path, budget: int):
    """Videoni bir marta skanerlab (ffprobe, qayta kodlashsiz), har bir qism
    `budget` baytdan oshmaydigan kesish nuqtalarini KALIT KADRLARDA tanlaydi.
    Nuqtalar vaqt emas, video kadrining tartib raqami (segment muxer'ning
    -segment_frames'i uchun): vaqt bilan kesishda konteyner vaqt siljishlari
    (masalan, qismlardan qayta yig'ilgan videoda audio "priming" tufayli 23 ms)
    kalit kadrni o'tkazib yuborib, qismni limitdan oshirib yuborardi.
    `-c copy` bilan video faqat kalit kadrda kesilishi mumkin - shuning uchun
    o'rtacha bitreytdan "har X soniyada kes" deb hisoblash ishonchsiz: ma'ruza/
    ekran yozuvlarida kalit kadrlar siyrak bo'lsa, ffmpeg kesish nuqtalarini
    o'tkazib yuborib qismlarni birlashtirib yuboradi, notekis bitreytda esa
    qismlar limitdan oshib ketadi. ffprobe topilmasa None qaytaradi."""
    ffprobe = _ffprobe_exe()
    if not ffprobe:
        return None
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=index",
         "-of", "default=noprint_wrappers=1", str(input_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore", timeout=FFMPEG_PROBE_TIMEOUT)
    info = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    if "index" not in info:
        return None
    video_index = info["index"]

    proc = subprocess.Popen(
        [ffprobe, "-v", "error", "-show_entries", "packet=stream_index,pts_time,size,flags",
         "-of", "csv=p=0", str(input_path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="ignore")
    points = []  # (kalit kadrning video kadrlari orasidagi tartib raqami, shu kadrgacha bo'lgan baytlar)
    total = 0
    video_packets = 0
    for line in proc.stdout:
        fields = line.strip().split(",")
        if len(fields) < 4:
            continue
        stream_index, _pts_time, size, flags = fields[:4]
        if stream_index == video_index:
            if flags.startswith("K"):
                points.append((video_packets, total))
            video_packets += 1
        if size.isdigit():
            total += int(size)
    if proc.wait() != 0 or not points:
        return None

    cuts = []
    seg_start = 0
    candidate = None
    for t, offset in points[1:] + [(None, total)]:
        if offset - seg_start > budget and candidate is not None:
            cuts.append(candidate[0])
            seg_start = candidate[1]
            candidate = None
        if offset - seg_start > budget:
            raise RuntimeError(
                f"Videoning ikki kalit kadri orasidagi qismi ({(offset - seg_start) / 1024**3:.2f} GB) "
                f"limitdan katta - bunday videoni qayta kodlamasdan bo'lib bo'lmaydi.")
        if t is not None:
            candidate = (t, offset)
    return cuts


def split_video_by_size(input_path: Path, out_dir: Path, max_bytes: int = TELEGRAM_MAX_PART_BYTES) -> list:
    """Video faylni max_bytes'dan oshmaydigan kerakli miqdordagi qismga bo'ladi
    (qayta kodlamasdan, -c copy - tez ishlaydi, sifat yo'qolmaydi). Fayl hajmi
    allaqachon max_bytes'dan kichik bo'lsa, o'zgarishsiz [input_path] qaytaradi."""
    input_path = Path(input_path)
    size = input_path.stat().st_size
    if size <= max_bytes:
        return [input_path]

    out_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(out_dir).free
    if free < size + 1024 ** 3:
        raise RuntimeError(
            f"Serverda joy yetarli emas: videoni bo'lish uchun {(size + 1024 ** 3) / 1024 ** 3:.1f} GB bo'sh joy "
            f"kerak, hozir {free / 1024 ** 3:.1f} GB bor.")

    # Konteyner (moov) sarlavhasi uchun 2% zaxira - paketlar yig'indisiga qo'shiladi.
    cuts = _plan_keyframe_cuts(input_path, int(max_bytes * 0.98))
    if cuts is not None:
        pattern = str(out_dir / "part_%03d.mp4")
        cmd = [ffmpeg_exe(), "-y", "-i", str(input_path), "-c", "copy", "-map", "0", "-f", "segment",
               "-reset_timestamps", "1"]
        if cuts:
            # Muxer aynan shu tartib raqamli (kalit) kadrda yangi qism boshlaydi.
            cmd += ["-segment_frames", ",".join(str(c) for c in cuts)]
        else:
            cmd += ["-segment_time", "1000000"]
        cmd.append(pattern)
        # 20-30 GB'lik faylni nusxalash sekin diskda 30 daqiqadan oshishi mumkin -
        # vaqt chegarasi hajmga qarab (kamida 10 MB/s tezlik faraz qilinadi).
        _run_ffmpeg(cmd, "videoni bo'laklarga bo'lish", timeout=max(FFMPEG_TIMEOUT, size // (10 * 1024 ** 2)))
        parts = sorted(out_dir.glob("part_*.mp4"))
        if not parts:
            raise RuntimeError("Video bo'laklarga bo'linmadi (natija fayllar topilmadi).")
        oversized = [p for p in parts if p.stat().st_size > max_bytes]
        if oversized:
            raise RuntimeError(
                f"{len(oversized)} ta bo'lak {max_bytes // (1024**2)} MB limitdan katta chiqdi - "
                f"qo'lda kichikroq qismlarga bo'lib yuklang.")
        return parts

    duration = get_duration_seconds(input_path)
    if duration <= 0:
        raise RuntimeError("Video davomiyligini aniqlab bo'lmadi, bo'laklarga bo'lib bo'lmaydi.")

    target_part_bytes = max_bytes * 0.9
    num_parts = max(2, math.ceil(size / target_part_bytes))
    part_duration = duration / num_parts

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


def join_video_parts(parts: list, output_path: Path, expected_duration: float = 0) -> None:
    """split_video_by_size qismlarini qayta bitta videoga yig'adi ("Asliga
    qaytarish"). Qayta kodlanmaydi (-c copy), shuning uchun sifat o'zgarmaydi.
    Avval vaqtinchalik faylga yoziladi - xatoda qismlar va eski holat saqlanadi."""
    parts = [Path(p) for p in parts]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total = sum(p.stat().st_size for p in parts)
    free = shutil.disk_usage(output_path.parent).free
    if free < total + 512 * 1024 ** 2:
        raise RuntimeError(
            f"Serverda joy yetarli emas: asliga qaytarish uchun {(total + 512 * 1024 ** 2) / 1024 ** 3:.1f} GB "
            f"bo'sh joy kerak, hozir {free / 1024 ** 3:.1f} GB bor.")

    tmp_path = output_path.with_name(output_path.stem + ".joining" + output_path.suffix)
    list_path = output_path.with_name(output_path.stem + ".parts.txt")
    quote = lambda p: "'" + str(p.resolve()).replace("'", "'\\''") + "'"
    list_path.write_text("".join(f"file {quote(p)}\n" for p in parts), encoding="utf-8")
    try:
        cmd = [ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
               "-c", "copy", "-map", "0", str(tmp_path)]
        _run_ffmpeg(cmd, "qismlarni bitta videoga yig'ish", timeout=max(FFMPEG_TIMEOUT, total // (10 * 1024 ** 2)))
        if not tmp_path.exists() or tmp_path.stat().st_size < total * 0.9:
            raise RuntimeError("Yig'ilgan video fayli to'liq emas.")
        if expected_duration and expected_duration > 0:
            got = get_duration_seconds(tmp_path)
            if abs(got - expected_duration) > max(3.0, expected_duration * 0.01):
                raise RuntimeError(f"Yig'ilgan video davomiyligi mos kelmadi ({got:.0f}s, kutilgan "
                                   f"{expected_duration:.0f}s).")
        tmp_path.replace(output_path)
    finally:
        list_path.unlink(missing_ok=True)
        tmp_path.unlink(missing_ok=True)


# --- Vaqt chizig'i: manba (original) vaqt <-> yakuniy (o'zbekcha video) vaqt ---
#
# Vaqt nuqtalari (bazada "freeze_points" ustunida saqlanadi):
#   {"type": "slow", "start": a, "end": b, "extra": e} - manbadagi [a, b] oralig'i
#       yakuniy videoda (b - a) + e soniyada ko'rsatiladi (video sekinlashadi);
#   {"type": "freeze", "time": t, "duration": d} - t nuqtada kadr d soniya kutadi.
#       Eski format {"time", "duration"} (type'siz) ham freeze deb o'qiladi.
#
# Barcha joylar (render, TTS joylash, barcha subtitr yozuvchilar, Learning,
# pleyer) vaqtni FAQAT source_time_to_final_time / final_time_to_source_time
# orqali hisoblaydi - aks holda audio, video va subtitr orasida farq paydo bo'ladi.

FREEZE_MIN_SEC = 0.05  # bundan qisqa freeze render qilinmaydi (bir kadrdan ham kam)
SLOW_MIN_EXTRA_SEC = 0.005


class _ActivePoints(list):
    """active_timeline_points natijasi (qayta normallashtirish shart emas)."""


def normalize_timeline_point(point: dict):
    """Bitta nuqtani yagona ko'rinishga keltiradi. Yaroqsiz bo'lsa None."""
    if not isinstance(point, dict):
        return None
    try:
        if point.get("type") == "slow":
            start = float(point.get("start") or 0.0)
            end = float(point.get("end") or 0.0)
            extra = float(point.get("extra") or 0.0)
            if end - start <= 0.001 or extra <= 0:
                return None
            return {"type": "slow", "start": round(start, 3), "end": round(end, 3), "extra": round(extra, 3)}
        duration = float(point.get("duration") or 0.0)
        if duration <= 0:
            return None
        return {"type": "freeze", "time": round(float(point.get("time") or 0.0), 3), "duration": round(duration, 3)}
    except (TypeError, ValueError):
        return None


def point_extra(point: dict) -> float:
    """Nuqta yakuniy videoga qo'shadigan vaqt (soniya)."""
    return (point.get("extra") if point.get("type") == "slow" else point.get("duration")) or 0.0


def point_position(point: dict) -> float:
    return point["start"] if point.get("type") == "slow" else point["time"]


def active_timeline_points(points: list) -> list:
    """Render va barcha vaqt hisoblari ishlatadigan nuqtalar: yaroqli, juda
    kichiklari tashlangan, manba vaqti bo'yicha tartiblangan."""
    if isinstance(points, _ActivePoints):
        return points
    result = _ActivePoints()
    for p in points or []:
        n = normalize_timeline_point(p)
        if not n:
            continue
        if n["type"] == "freeze" and n["duration"] <= FREEZE_MIN_SEC:
            continue
        if n["type"] == "slow" and n["extra"] <= SLOW_MIN_EXTRA_SEC:
            continue
        result.append(n)
    # Bir xil joyda freeze slow'dan oldin: slow [a, b] + freeze(b) + slow [b, c] zanjiri.
    result.sort(key=lambda p: (point_position(p), 0 if p["type"] == "freeze" else 1))
    return result


def _timeline_shift(t: float, points: list) -> float:
    shift = 0.0
    for p in points:
        if p["type"] == "slow":
            a, b = p["start"], p["end"]
            if t >= b:
                shift += p["extra"]
            elif t > a:
                shift += p["extra"] * (t - a) / (b - a)
        elif p["time"] <= t:
            shift += p["duration"]
    return shift


def source_time_to_final_time(source_time: float, points: list) -> float:
    """Original (manba) vaqtni yakuniy (sekinlashtirilgan/kutishli) video
    vaqtiga o'giradi. Yagona manba: TTS joylash, render, SRT/VTT, Learning va
    pleyer shu formuladan foydalanadi.

    freeze: time <= t bo'lsa +duration; slow: t >= end bo'lsa +extra,
    start < t < end bo'lsa +extra*(t-start)/(end-start)."""
    source_time = source_time or 0.0
    active = active_timeline_points(points)
    if not active:
        return round(source_time, 3)
    return round(source_time + _timeline_shift(source_time, active), 3)


def final_time_to_source_time(final_time: float, points: list) -> float:
    """source_time_to_final_time ning teskarisi. Freeze (kutish) ichidagi
    yakuniy vaqt shu freeze nuqtasining manba vaqtiga tushadi."""
    final_time = max(final_time or 0.0, 0.0)
    active = active_timeline_points(points)
    if not active:
        return round(final_time, 3)
    lo, hi = max(final_time - sum(point_extra(p) for p in active), 0.0), final_time
    for _ in range(60):
        mid = (lo + hi) / 2
        if mid + _timeline_shift(mid, active) <= final_time + 1e-9:
            lo = mid
        else:
            hi = mid
    return round(lo, 3)


def total_timeline_extra(points: list) -> float:
    """Barcha nuqtalar yakuniy videoga qo'shadigan vaqt yig'indisi (soniya)."""
    return round(sum(point_extra(p) for p in active_timeline_points(points)), 3)


def total_freeze_duration(freeze_points: list) -> float:
    """Eski nom (orqaga moslik) - total_timeline_extra bilan bir xil."""
    return total_timeline_extra(freeze_points)


def timeline_summary(points: list) -> dict:
    """Foydalanuvchiga ko'rsatish uchun: nechta sekinlashtirish va kutish."""
    active = active_timeline_points(points)
    slows = [p for p in active if p["type"] == "slow"]
    freezes = [p for p in active if p["type"] == "freeze"]
    return {
        "slow_count": len(slows), "slow_extra": round(sum(p["extra"] for p in slows), 2),
        "freeze_count": len(freezes), "freeze_total": round(sum(p["duration"] for p in freezes), 2),
        "total_extra": round(sum(point_extra(p) for p in active), 2),
    }


def timeline_message(points: list) -> str:
    """"N ta joyda video sekinlashtirildi (jami X s), M ta joyda kutish"."""
    s = timeline_summary(points)
    parts = []
    if s["slow_count"]:
        parts.append(f"{s['slow_count']} ta joyda video sekinlashtirildi (jami {s['slow_extra']:.1f} s)")
    if s["freeze_count"]:
        parts.append(f"{s['freeze_count']} ta joyda kutish ({s['freeze_total']:.1f} s)")
    return ", ".join(parts)


def apply_freeze_to_segments(segments: list, freeze_points: list) -> list:
    """SRT/VTT segmentlarni ({"start","end","text"}) yakuniy vaqt chizig'iga
    o'tkazadi. Original ro'yxat o'zgarmaydi - yangi ro'yxat qaytadi."""
    active = active_timeline_points(freeze_points)
    if not active:
        return segments
    result = []
    for s in segments:
        result.append({
            **s,
            "start": source_time_to_final_time(s["start"], active),
            "end": source_time_to_final_time(s["end"], active),
        })
    return result


def final_segments_to_source(segments: list, points: list) -> list:
    """apply_freeze_to_segments ning teskarisi (yakuniy -> manba vaqt)."""
    active = active_timeline_points(points)
    if not active:
        return segments
    return [{**s, "start": final_time_to_source_time(s["start"], active),
             "end": final_time_to_source_time(s["end"], active)} for s in segments]


def mux_video_audio(video_path: Path, audio_path: Path, out_path: Path, target_duration: float = None):
    """Original videoning tasvirini saqlab, audio yo'lini yangi audio bilan almashtiradi.

    target_duration berilsa (original video davomiyligi + freeze'lar yig'indisi),
    "-shortest" o'rniga "-t" ishlatiladi: bu qisqaroq audio videoni "qisib
    qo'ymasligini" kafolatlaydi (audio tugagach video davom etadi / jim qoladi),
    va shu bilan birga yakuniy fayl kutilganidan uzunroq chiqib ketmasligini
    ta'minlaydi (xavfsizlik yopig'i)."""
    cmd = [
        ffmpeg_exe(), "-y", "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
    ]
    if target_duration and target_duration > 0:
        cmd += ["-t", f"{target_duration:.3f}"]
    else:
        cmd += ["-shortest"]
    cmd += [str(out_path)]
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


# Subtitr "kuydirish" (hardsub) uchun standart stil - ekranning pastida,
# oq matn, qalin qora kontur (har qanday video foni ustida o'qilishi uchun).
SUBTITLE_BURN_STYLE = (
    "FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=36"
)


def build_subtitle_burn_cmd(video_path: Path, srt_path: Path, out_path: Path) -> list:
    """ffmpeg buyrug'ini quradi. `subtitles=` filtri argumentidagi maxsus
    belgilar (masalan ':') bilan bog'liq escaping muammosidan qochish uchun,
    SRT fayli NISBIY (faqat fayl nomi) ko'rsatiladi - shuning uchun bu buyruq
    albatta cwd=srt_path.parent bilan ishga tushirilishi SHART
    (burn_subtitles_into_video shuni qiladi)."""
    return [
        ffmpeg_exe(), "-y", "-i", str(video_path),
        "-vf", f"subtitles={srt_path.name}:force_style='{SUBTITLE_BURN_STYLE}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out_path),
    ]


def burn_subtitles_into_video(video_path: Path, srt_path: Path, out_path: Path):
    """Berilgan SRT faylini video piksellariga "kuydiradi" (hardsub) - asl
    video o'zgarishsiz qoladi, faqat YANGI fayl yaratiladi. Video qayta
    kodlanadi (subtitr filtri shuni talab qiladi), audio esa o'zgarishsiz
    ko'chiriladi (-c:a copy)."""
    cmd = build_subtitle_burn_cmd(video_path, srt_path, out_path)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=FFMPEG_TIMEOUT, cwd=str(srt_path.parent))
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg (subtitr kuydirish) {FFMPEG_TIMEOUT // 60} daqiqadan ortiq davom etdi va to'xtatildi. "
            f"Qayta urinib ko'ring."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi (subtitr kuydirish): {(proc.stdout or '')[-2000:]}")
    if not out_path.exists() or out_path.stat().st_size < 1024:
        raise RuntimeError("Subtitrli video fayli yaratilmadi yoki bo'sh.")


def _run_ffmpeg(cmd: list, description: str, timeout: int = FFMPEG_TIMEOUT):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg ({description}) {timeout // 60} daqiqadan ortiq davom etdi va to'xtatildi. "
            f"Qayta urinib ko'ring."
        )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi ({description}): {(proc.stdout or '')[-1500:]}")


def _detect_fps(ffmpeg_info: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", ffmpeg_info or "")
    return float(m.group(1)) if m else 25.0


# Bitta setpts filtridagi eng ko'p nuqta (ifoda juda uzun bo'lmasin).
_TIMELINE_GROUP_SIZE = 40


def _timeline_point_end(p: dict) -> float:
    return p["end"] if p["type"] == "slow" else p["time"]


def _group_timeline_points(active: list) -> list:
    """Nuqtalarni setpts guruhlariga bo'ladi. Guruh chegarasi faqat oldingi
    nuqtalarning hammasi keyingi nuqta boshlanishidan oldin tugagan joyda
    qo'yiladi - shunda keyingi guruh uchun oldingi siljish doimiy (C)."""
    groups, current, max_end = [], [], float("-inf")
    for p in active:
        if len(current) >= _TIMELINE_GROUP_SIZE and max_end <= point_position(p):
            groups.append(current)
            current = []
        current.append(p)
        max_end = max(max_end, _timeline_point_end(p))
    if current:
        groups.append(current)
    return groups


def build_timeline_video_filter(points: list, fps: float, pad_sec: float = 0.0) -> str:
    """Manba videoni yakuniy vaqt chizig'iga o'tkazadigan ffmpeg video filtri.

    Har kadr vaqti T -> T + siljish(T) (source_time_to_final_time bilan bir xil
    formula); keyin fps filtri bo'sh joylarni oldingi kadr bilan to'ldiradi:
    slow oralig'ida kadrlar siyraklashadi (sekinlashish), freeze joyida esa
    bitta kadr takrorlanadi. Hammasi bitta o'tishda - bo'laklarga bo'lib
    qayta yig'ishdagi kadr yaxlitlash xatolari to'planmaydi."""
    active = active_timeline_points(points)
    chain = ["setpts=PTS-STARTPTS"]
    offset = 0.0
    for group in _group_timeline_points(active):
        terms = []
        for p in group:
            if p["type"] == "slow":
                a = p["start"] + offset
                terms.append(f"{p['extra']:.6f}*clip((T-{a:.6f})/{p['end'] - p['start']:.6f}\\,0\\,1)")
            else:
                terms.append(f"{p['duration']:.6f}*gte(T\\,{p['time'] + offset:.6f})")
        chain.append("setpts=(T+" + "+".join(terms) + ")/TB")
        offset += sum(point_extra(p) for p in group)
    if pad_sec > 0:
        chain.append(f"tpad=stop_mode=clone:stop_duration={pad_sec:.3f}")
    chain.append(f"fps={fps:g}")
    chain.append("format=yuv420p")
    return ",".join(chain)


def mux_video_audio_with_freezes(video_path: Path, audio_path: Path, out_path: Path,
                                  freeze_points: list, work_dir: Path, target_duration: float = None):
    """Yakuniy videoni yig'adi: original tasvir + yangi (o'zbekcha) audio.

    Vaqt nuqtalari (slow/freeze) bo'lsa video bitta ffmpeg o'tishida qayta
    kodlanadi: slow oraliqlari sekinlashadi, freeze joylarida kadr kutadi.
    Nuqta bo'lmasa - tez, qayta kodlanmaydigan mux. Original audio ishlatilmaydi.

    target_duration - kutilgan aniq davomiylik (original + barcha extra/duration)."""
    active = active_timeline_points(freeze_points)
    if not active:
        mux_video_audio(video_path, audio_path, out_path, target_duration=target_duration)
        return

    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        probe = subprocess.run([ffmpeg_exe(), "-i", str(video_path)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="ignore",
                                timeout=FFMPEG_PROBE_TIMEOUT)
        fps = _detect_fps(probe.stdout)
    except subprocess.TimeoutExpired:
        fps = 25.0
    total_duration = get_duration_seconds(video_path)
    extra = sum(point_extra(p) for p in active)
    if not target_duration or target_duration <= 0:
        target_duration = total_duration + extra

    vf = build_timeline_video_filter(active, fps, pad_sec=extra + 1.0)
    filter_path = work_dir / "timeline_filter.txt"
    filter_path.write_text(vf, encoding="utf-8")
    tmp_path = out_path.with_name(out_path.stem + ".render" + out_path.suffix)
    cmd = [
        ffmpeg_exe(), "-y", "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
    ]
    # Juda ko'p nuqtali uzun filtr buyruq qatoriga sig'masligi mumkin - fayldan o'qiladi.
    cmd += ["-filter_script:v", str(filter_path)] if len(vf) > 60000 else ["-vf", vf]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{target_duration:.3f}",
        "-movflags", "+faststart", str(tmp_path),
    ]
    try:
        _run_ffmpeg(cmd, "videoni sekinlashtirish va audio bilan yig'ish",
                    timeout=max(FFMPEG_TIMEOUT, int(total_duration * 2)))
        if not tmp_path.exists() or tmp_path.stat().st_size < 1024:
            raise RuntimeError("Yakuniy video fayli yaratilmadi yoki bo'sh.")
        got = get_duration_seconds(tmp_path)
        if got and abs(got - target_duration) > max(1.0, target_duration * 0.005):
            raise RuntimeError(f"Yakuniy video davomiyligi mos kelmadi ({got:.2f}s, kutilgan "
                               f"{target_duration:.2f}s).")
        tmp_path.replace(out_path)
    finally:
        tmp_path.unlink(missing_ok=True)
        filter_path.unlink(missing_ok=True)


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


def split_audio_into_pieces(input_path: Path, out_dir: Path, piece_seconds: int = 45) -> list:
    """Audio faylni (masalan bitta 5 daqiqalik bo'lakning mp3'ini) piece_seconds
    soniyalik vaqtinchalik kichik qismlarga bo'ladi - bo'lakni qayta ishlashda
    (retry) Whisper aniqligini oshirish uchun. Original fayl (input_path) hech
    qachon o'zgartirilmaydi yoki o'chirilmaydi - faqat out_dir ichida yangi
    vaqtinchalik qism fayllar yaratiladi. Qaytaradi: [(Path, duration_seconds), ...]
    vaqt tartibida."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "piece_%03d.mp3")
    cmd = [
        ffmpeg_exe(), "-y", "-i", str(input_path),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
        "-f", "segment", "-segment_time", str(piece_seconds), "-reset_timestamps", "1",
        pattern,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                               timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg bo'lakni kichik qismlarga bo'lishda juda uzoq davom etdi va to'xtatildi.")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg xatosi (kichik qismlarga bo'lish): {(proc.stdout or '')[-1000:]}")
    piece_files = sorted(out_dir.glob("piece_*.mp3"))
    if not piece_files:
        raise RuntimeError("Bo'lak kichik qismlarga bo'linmadi.")
    return [(p, get_duration_seconds(p)) for p in piece_files]


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
    elif language:
        # Lug'atda ru/uz/en'dan boshqa til uchun yozuv yo'q - ru/en atamalarini
        # qo'shish bu tilning promptini chalkashtirib yuboradi, shuning uchun faqat
        # asosiy ko'rsatma ishlatiladi.
        glossary_prompt = ""
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

def _split_ms(total_sec: float):
    """Vaqtni soat/daqiqa/soniya/ms ga ajratadi. Avval butun millisekundga
    yaxlitlanadi - aks holda 1.9996 s "00:00:01,1000" bo'lib qolardi."""
    ms_total = int(round(max(total_sec or 0.0, 0.0) * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return h, m, s, ms


def fmt_srt_time(total_sec: float) -> str:
    h, m, s, ms = _split_ms(total_sec)
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
    h, m, s, ms = _split_ms(total_sec)
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
