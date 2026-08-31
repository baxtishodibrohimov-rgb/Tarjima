"""
PWA ikonkalarini generatsiya qiladi (icon-192.png, icon-512.png, apple-touch-icon.png).

Tashqi kutubxona (Pillow, ffmpeg) ishlatmaydi - faqat standart kutubxona (zlib, struct)
bilan PNG faylni to'g'ridan-to'g'ri qurib chiqadi. Bir martalik skript: ikonka rangini
yoki dizaynini o'zgartirmoqchi bo'lsangiz shu faylni tahrirlab qayta ishga tushiring:

    python3 scripts/generate_pwa_icons.py
"""
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"

ACCENT = (0xb4, 0x5a, 0x34)   # site --accent
BG = ACCENT
FG = (0xff, 0xff, 0xff)


def _write_png(path: Path, size: int, pixels):
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type: None
        for x in range(size):
            r, g, b, a = pixels[y][x]
            raw += bytes((r, g, b, a))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def _rounded_square_contains(x, y, size, radius):
    cx = min(max(x, radius), size - 1 - radius)
    cy = min(max(y, radius), size - 1 - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _triangle_contains(px, py, p1, p2, p3):
    def sign(a, b, c):
        return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])
    d1 = sign((px, py), p1, p2)
    d2 = sign((px, py), p2, p3)
    d3 = sign((px, py), p3, p1)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def make_icon(size: int, opaque: bool = False) -> list:
    radius = int(size * 0.22)
    # Play-tugma uchburchagi - video/audio studiyasini anglatadi, markazdan biroz o'ngga
    # siljitilgan (ko'zga tabiiy markazda ko'rinishi uchun, chunki uchburchak og'irligi chapga tortadi)
    cx, cy = size * 0.47, size * 0.5
    tri_h = size * 0.34
    tri_w = size * 0.30
    p1 = (cx - tri_w / 2, cy - tri_h / 2)
    p2 = (cx - tri_w / 2, cy + tri_h / 2)
    p3 = (cx + tri_w / 2, cy)

    pixels = []
    for y in range(size):
        row = []
        for x in range(size):
            inside_bg = True if opaque else _rounded_square_contains(x, y, size, radius)
            if not inside_bg:
                row.append((0, 0, 0, 0))
                continue
            if _triangle_contains(x + 0.5, y + 0.5, p1, p2, p3):
                row.append((*FG, 255))
            else:
                row.append((*BG, 255))
        pixels.append(row)
    return pixels


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_png(OUT_DIR / "icon-192.png", 192, make_icon(192))
    _write_png(OUT_DIR / "icon-512.png", 512, make_icon(512))
    # Apple ekranda shaffoflikni qora rangga bo'yab ko'rsatadi, shuning uchun
    # apple-touch-icon har doim to'liq kvadrat (shaffof burchaklarsiz) bo'lishi kerak
    _write_png(OUT_DIR / "apple-touch-icon.png", 180, make_icon(180, opaque=True))
    print(f"Ikonkalar yaratildi: {OUT_DIR}")


if __name__ == "__main__":
    main()
