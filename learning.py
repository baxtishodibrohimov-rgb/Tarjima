"""
"Ruscha o'rganish" so'zlar treki: Learning SRT teglaridan (translation.parse_learning_srt)
WebVTT/ASS yaratish, ASOS fayl nomlari, Learning videosini ffmpeg orqali o'qish,
so'zlarni kadrga kuydirish va intro bilan ulash.

Vaqtlar Learning subtitrlari bilan BITTA manbadan hisoblanadi:
transcription.source_time_to_final_time (freeze-point), so'ng intro surilishi.
"""
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

import transcription

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "fonts"

YANGI_COLOR_HEX = "#FFD400"
TAKROR_COLOR_HEX = "#FFFFFF"


# ---------------------------------------------------------------------------
#                       VAQTLAR (freeze-point + intro)
# ---------------------------------------------------------------------------

def shift_time(source_time: float, freeze_points: list, offset: float = 0.0) -> float:
    """Avval mavjud freeze-point o'zgartirishi, keyin intro surilishi."""
    return round(transcription.source_time_to_final_time(source_time, freeze_points or []) + (offset or 0.0), 3)


def words_cues(blocks: list, freeze_points: list = None, offset: float = 0.0) -> list:
    """Har tegli blok uchun bitta cue - blokning o'z vaqtida. Tegsiz blok tashlab ketiladi."""
    cues = []
    for b in blocks:
        if not b.get("words"):
            continue
        cues.append({
            "start": shift_time(b["start"], freeze_points, offset),
            "end": shift_time(b["end"], freeze_points, offset),
            "words": b["words"],
        })
    return cues


def shifted_segments(segments: list, freeze_points: list = None, offset: float = 0.0) -> list:
    return [{**s, "start": shift_time(s["start"], freeze_points, offset),
             "end": shift_time(s["end"], freeze_points, offset)} for s in segments]


# ---------------------------------------------------------------------------
#                                 WebVTT
# ---------------------------------------------------------------------------

def _vtt_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_words_vtt(cues: list) -> str:
    lines = [
        "WEBVTT", "",
        "STYLE",
        f"::cue(.yangi) {{ color: {YANGI_COLOR_HEX}; }}",
        f"::cue(.takror) {{ color: {TAKROR_COLOR_HEX}; }}",
        "",
    ]
    for i, c in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(f"{transcription.fmt_vtt_time(c['start'])} --> {transcription.fmt_vtt_time(c['end'])} "
                     f"line:5% position:95% align:end")
        # Bitta blokdagi so'zlar alohida satrlarga tushmaydi: video tepasida
        # chapdan o'ngga bitta gorizontal qator bo'lib ko'rinadi.
        parts = [f"<c.{w['kind']}>{_vtt_escape(w['lemma'])} — {_vtt_escape(w['meaning'])}</c>"
                 for w in c["words"]]
        lines.append("   •   ".join(parts))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#                                   ASS
# ---------------------------------------------------------------------------

def _ass_time(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(s: str) -> str:
    return s.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ass_colour(hex_rgb: str) -> str:
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H00{b}{g}{r}".upper()


def build_words_ass(cues: list, width: int, height: int) -> str:
    """Yuqori o'ng burchak (Alignment 9), Yangi - sariq, Takror - oq."""
    font_size = max(int(height * 0.045), 16)
    outline = max(round(height / 360), 2)
    margin_r = int(width * 0.03)
    margin_v = int(height * 0.04)
    style = ("{name},DejaVu Sans,{fs},{col},&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,"
             "{ol},1,9,{ml},{mr},{mv},1")
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        "Style: " + style.format(name="Yangi", fs=font_size, col=_ass_colour(YANGI_COLOR_HEX), ol=outline,
                                 ml=margin_r, mr=margin_r, mv=margin_v),
        "Style: " + style.format(name="Takror", fs=font_size, col=_ass_colour(TAKROR_COLOR_HEX), ol=outline,
                                 ml=margin_r, mr=margin_r, mv=margin_v),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    for c in cues:
        parts = []
        for w in c["words"]:
            style_name = "Yangi" if w["kind"] == "yangi" else "Takror"
            parts.append(f"{{\\r{style_name}}}{_ass_escape(w['lemma'])} — {_ass_escape(w['meaning'])}")
        first = "Yangi" if c["words"][0]["kind"] == "yangi" else "Takror"
        # Yuklab olinadigan/hardsub videoda ham xuddi pleyerdagidek gorizontal.
        events.append(f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},{first},,0,0,0,,"
                      + "   •   ".join(parts))
    return "\n".join(head + events) + "\n"


# ---------------------------------------------------------------------------
#                              FAYL NOMLARI
# ---------------------------------------------------------------------------

def asos_name(srt_filename: str, fallback: str = "learning") -> str:
    """ASOS = Learning SRT nomi kengaytmasiz, oxiridagi _LEARNING (har qanday registr) olib tashlanadi."""
    name = Path(srt_filename or "").name
    stem = re.sub(r"\.srt$", "", name, flags=re.IGNORECASE)
    stem = re.sub(r"_learning$", "", stem, flags=re.IGNORECASE).strip()
    return stem or fallback


def content_disposition(filename: str) -> str:
    """RFC 5987: lotin bo'lmagan va apostrofli nomlar ham to'g'ri yuklanadi."""
    ascii_name = filename.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'["\\]', "", ascii_name).strip() or "download"
    if ascii_name.startswith("."):
        ascii_name = "download" + ascii_name
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


# ---------------------------------------------------------------------------
#                         FFMPEG: o'qish va tekshirish
# ---------------------------------------------------------------------------

_FPS_RATIONAL = {"23.98": "24000/1001", "29.97": "30000/1001", "59.94": "60000/1001", "47.95": "48000/1001"}
_CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "4.0": 4, "5.0": 5, "5.1": 6, "5.1(side)": 6,
             "6.1": 7, "7.1": 8}
_ENCODER_FOR_CODEC = {"h264": "libx264", "hevc": "libx265", "mpeg4": "mpeg4"}

_ass_supported_cache = {}


def probe_media(path: Path) -> dict:
    """ffprobe imageio-ffmpeg build'ida yo'q - shuning uchun `ffmpeg -i` chiqishidan
    video/audio parametrlarini o'qiydi."""
    proc = subprocess.run([transcription.ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                          timeout=transcription.FFMPEG_PROBE_TIMEOUT)
    out = proc.stdout or ""
    info = {"duration": transcription.get_duration_seconds(path), "audio": []}
    vm = re.search(r"Stream #\S+.*?: Video: (\w+)[^\n]*", out)
    if not vm:
        raise RuntimeError(f"Videoda tasvir oqimi topilmadi: {path.name}")
    vline = vm.group(0)
    info["video_codec"] = vm.group(1)
    pm = re.search(r"Video: \w+[^,]*, ([a-z0-9_]+)", vline)
    info["pix_fmt"] = pm.group(1) if pm else "yuv420p"
    sm = re.search(r", (\d{2,5})x(\d{2,5})", vline)
    if not sm:
        raise RuntimeError(f"Video o'lchami aniqlanmadi: {path.name}")
    info["width"], info["height"] = int(sm.group(1)), int(sm.group(2))
    fm = re.search(r"([\d.]+) fps", vline) or re.search(r"([\d.]+) tbr", vline)
    fps = fm.group(1) if fm else "25"
    info["fps"] = _FPS_RATIONAL.get(fps, fps)
    tm = re.search(r"([\d.]+)(k?) tbn", vline)
    info["tbn"] = int(float(tm.group(1)) * (1000 if tm.group(2) else 1)) if tm else None
    for am in re.finditer(r"Stream #\S+.*?: Audio: (\w+)[^,\n]*, (\d+) Hz, ([^,\n]+)", out):
        layout = am.group(3).strip()
        ch = _CHANNELS.get(layout)
        if ch is None:
            cm = re.match(r"(\d+) channels", layout)
            ch = int(cm.group(1)) if cm else 2
        info["audio"].append({"codec": am.group(1), "sample_rate": int(am.group(2)), "channels": ch})
    return info


def fps_float(fps: str) -> float:
    if "/" in str(fps):
        a, b = str(fps).split("/")
        return float(a) / float(b)
    return float(fps)


def video_encoder_for(info: dict) -> str:
    return _ENCODER_FOR_CODEC.get(info["video_codec"], "libx264")


def ass_filter_available() -> bool:
    """ffmpeg build'ida libass (ass filtri) bormi - natija keshlanadi."""
    exe = transcription.ffmpeg_exe()
    if exe not in _ass_supported_cache:
        try:
            proc = subprocess.run([exe, "-hide_banner", "-filters"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="ignore",
                                  timeout=transcription.FFMPEG_PROBE_TIMEOUT)
            _ass_supported_cache[exe] = bool(re.search(r"^\s*\S+\s+ass\s", proc.stdout or "", re.MULTILINE))
        except Exception:
            _ass_supported_cache[exe] = False
    return _ass_supported_cache[exe]


def params_match(a: dict, b: dict) -> bool:
    """Concat demuxer + -c copy uchun parametrlar to'liq mosmi."""
    keys = ("video_codec", "width", "height", "pix_fmt", "fps")
    if any(a.get(k) != b.get(k) for k in keys):
        return False
    return [(x["codec"], x["sample_rate"], x["channels"]) for x in a["audio"]] == \
        [(x["codec"], x["sample_rate"], x["channels"]) for x in b["audio"]]


def _filter_path(path: Path) -> str:
    """ffmpeg filter argumentidagi Windows drive ``:`` belgisini escape qiladi."""
    return Path(path).as_posix().replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_export(clean_video: Path, out_path: Path, work_dir: Path, info: dict,
                 ass_text: str = None, intro_video: Path = None) -> str:
    """Yuklab olinadigan Learning videosini yig'adi:
      - intro bo'lsa - Learning videosi boshiga ulanadi (parametrlar to'liq mos va
        kuydirish yo'q bo'lsa concat demuxer + -c copy, aks holda qayta kodlash);
      - ass_text bo'lsa - so'zlar kadr ustiga yoziladi (libass).
    Qaytaradi: qanday usul ishlatilgani (log uchun)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    exe = transcription.ffmpeg_exe()
    vfilter = None
    if ass_text:
        ass_path = work_dir / "sozlar.ass"
        ass_path.write_text(ass_text, encoding="utf-8")
        vfilter = f"ass='{_filter_path(Path(ass_path.name))}':fontsdir='{_filter_path(FONTS_DIR)}'"
    n_audio = len(info["audio"])
    venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    aenc = ["-c:a", "aac", "-b:a", "192k"]

    if intro_video is None:
        if vfilter is None:
            raise ValueError("Eksport uchun intro yoki so'zlar kerak.")
        cmd = [exe, "-y", "-i", str(clean_video), "-map", "0:v:0", "-map", "0:a?", "-vf", vfilter,
               *venc, "-c:a", "copy", "-movflags", "+faststart", str(out_path)]
        method = "so'zlar kuydirildi"
    else:
        intro_info = probe_media(intro_video)
        if vfilter is None and params_match(intro_info, info):
            lst = work_dir / "concat.txt"
            lst.write_text(f"file '{intro_video.resolve()}'\nfile '{Path(clean_video).resolve()}'\n",
                           encoding="utf-8")
            cmd = [exe, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0", "-c", "copy",
                   "-movflags", "+faststart", str(out_path)]
            method = "intro ulandi (concat demuxer, -c copy)"
        else:
            ins = "".join(f"[{i}:v:0]" + "".join(f"[{i}:a:{k}]" for k in range(n_audio)) for i in (0, 1))
            graph = f"{ins}concat=n=2:v=1:a={n_audio}[cv]" + "".join(f"[ca{k}]" for k in range(n_audio))
            graph += f";[cv]{vfilter}[vout]" if vfilter else ";[cv]null[vout]"
            maps = ["-map", "[vout]"] + [x for k in range(n_audio) for x in ("-map", f"[ca{k}]")]
            cmd = [exe, "-y", "-i", str(intro_video), "-i", str(clean_video), "-filter_complex", graph, *maps,
                   *venc, *(aenc if n_audio else []), "-movflags", "+faststart", str(out_path)]
            method = "intro ulandi (qayta kodlash" + (", so'zlar kuydirildi)" if vfilter else ")")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
                          timeout=transcription.FFMPEG_TIMEOUT, cwd=str(work_dir))
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 1024:
        raise RuntimeError(f"ffmpeg xatosi (Learning eksport): {(proc.stdout or '')[-1500:]}")
    return method
