"""MASTER INSTRUKSIYA v8 / LEARNING INSTRUKSIYA v3 bilan kelishilgan raqamlar.

Vaqt, ovoz tezligi, video sekinlashtirish va SRT bloklari bilan bog'liq barcha
chegaralar FAQAT shu yerda turadi - boshqa modullar shu konstantalarni import
qiladi (kod bo'ylab "sehrli raqamlar" tarqalmasin).

Asosiy tamoyil: har bir gapning o'zbekcha ovozi videoda shu gapning original
boshlanish joyida boshlanadi. Ovoz sig'masa - video moslashadi (sekinlashadi),
matn qisqartirilmaydi.
"""
import re

# Blok shu belgilardan biri bilan tugasa - gap tugadi (MASTER 14).
SENTENCE_END_CHARS = (".", "?", "!", "…")
# Tugash belgisidan keyin kelishi mumkin bo'lgan yopuvchi belgilar: ."  .)  !»
CLOSING_CHARS = "\"'»”’)]"

# O'zbekcha ovoz tezligi (tempo) oralig'i va ketma-ket gaplar orasidagi farq.
TEMPO_MIN = 0.90
TEMPO_MAX = 1.15
TEMPO_STEP = 0.03
# Lektor sur'atini o'lchash oynasi (gap markazidan +-45 s).
PACE_WINDOW_SEC = 90.0
# Butun video asosiy tezligi chegarasi (umuman tez gapiradigan lektor uchun).
BASE_TEMPO_MIN = 1.00
BASE_TEMPO_MAX = 1.10
# Videoni sekinlashtirishning eng past tezligi (0.75 = 75%).
SLOWMO_MIN_RATE = 0.75
# O'zbekcha TTS ning 1.0 tezlikdagi taxminiy o'qish tezligi (belgi/soniya) -
# faqat oldindan baho (ogohlantirish) uchun.
TTS_CPS_ESTIMATE = 14.0

# Original SRT bloklari: so'zlar orasidagi shu pauzadan uzun joyda blok bo'linadi.
PAUSE_SPLIT_SEC = 0.5
DISPLAY_MAX_SEC = 7.0
DISPLAY_MAX_CHARS = 90
# Gap tugash belgisi bilan yangi blok boshlash uchun blokning eng kam uzunligi.
MIN_SENTENCE_BLOCK_SEC = 1.0

# Provayder chegaralari.
ELEVENLABS_MAX_BYTES = 3 * 1024 ** 3
ELEVENLABS_MAX_SEC = 10 * 3600
OPENAI_MAX_BYTES = 25 * 1024 * 1024
# Bitta TTS so'rovidagi eng ko'p belgi (gap bundan uzun bo'lsa vergul/tire joyida bo'linadi).
TTS_MAX_CHARS = {"aisha": 1000, "openai": 2000}

_TAG_RE = re.compile(r"\[[^\]]*\]")


def ends_sentence(text: str) -> bool:
    """Blok matni gap tugash belgisi bilan tugaydimi (yopuvchi qavs/qo'shtirnoq
    va ichki teglar hisobga olinmaydi)."""
    t = _TAG_RE.sub("", text or "").rstrip()
    while t and t[-1] in CLOSING_CHARS:
        t = t[:-1].rstrip()
    return bool(t) and (t.endswith(SENTENCE_END_CHARS) or t.endswith("..."))
