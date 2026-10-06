"""Zip ichidagi fayl nomlari qanday yozilganini ko'rsatadi (nomlar "???" bo'lib
chiqqanda sababini topish uchun). Faqat o'qiydi, hech narsani o'zgartirmaydi.

    sudo python3 /opt/tarjima/scripts/zip_names_debug.py
(manzil berilmasa /opt/tarjima-storage/cloud ichidagi barcha zip'lar tekshiriladi)
"""
import glob
import struct
import sys
import zipfile


def extra_ids(extra: bytes) -> list:
    ids, pos = [], 0
    while pos + 4 <= len(extra):
        header_id, size = struct.unpack("<HH", extra[pos:pos + 4])
        ids.append(f"0x{header_id:04x}({size})")
        pos += 4 + size
    return ids


def main(path: str):
    with zipfile.ZipFile(path) as zf:
        print("=" * 60)
        print(f"{path}\nFayllar: {len(zf.infolist())}")
        for info in zf.infolist()[:4]:
            raw = info.filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437", errors="replace")
            zf.fp.seek(info.header_offset)
            local = zf.fp.read(30)
            name_len, extra_len = struct.unpack("<HH", local[26:30])
            local_flags = struct.unpack("<H", local[6:8])[0]
            local_name = zf.fp.read(name_len)
            local_extra = zf.fp.read(extra_len)
            print("-" * 60)
            print(f"flag=0x{info.flag_bits:04x} local_flag=0x{local_flags:04x} "
                  f"create_system={info.create_system} create_version={info.create_version}")
            print(f"markaziy nom hex: {raw[:48].hex(' ')}")
            print(f"lokal nom hex:    {local_name[:48].hex(' ')}")
            print(f"markaziy extra: {extra_ids(info.extra)}  lokal extra: {extra_ids(local_extra)}")


if __name__ == "__main__":
    paths = sys.argv[1:] or sorted(glob.glob("/opt/tarjima-storage/cloud/*/*.zip"))
    if not paths:
        print("Zip topilmadi.")
    for p in paths:
        main(p)
