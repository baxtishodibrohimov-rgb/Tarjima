"""Sinov skripti: bitta oy (va ixtiyoriy bitta shifokor) natijasini terminalga chiqaradi.

Misol:
    python sinov.py --oy 2026-11 --shifokor madina
    python sinov.py --oy 2026-11 --shifokor madina --foiz 25    # foizlar.json bo'lmasa ham
    python sinov.py --xodimlar                                  # xodimlar ro'yxati va ID'lari

Kutilgan natija (xulosadagi tekshiruv): madina, noyabr 2026 -> tushgan 500 000, maosh 125 000.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from client import CliniccardsClient
from report import staff_name
from service import HERE, Percents, Service, load_env


def fmt(v) -> str:
    return f"{v:,.2f}".replace(",", " ")


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--oy", help="YYYY-MM, masalan 2026-11")
    ap.add_argument("--shifokor", help="Shifokor ismi (qismi) yoki doctor_id")
    ap.add_argument("--foiz", type=float, help="Shu shifokor uchun foizni vaqtincha berish")
    ap.add_argument("--xodimlar", action="store_true", help="Xodimlar ro'yxatini chiqarish")
    ap.add_argument("--orqaga", type=int, default=int(os.environ.get("ORQAGA_OY", "24")),
                    help="Necha oy orqadagi ishlarni ko'rish (standart 24)")
    a = ap.parse_args()

    client = CliniccardsClient(os.environ.get("CLINICCARDS_TOKEN", ""))
    svc = Service(client, Percents(Path(os.environ.get("FOIZLAR_FAYL", HERE / "foizlar.json"))),
                  months_back=a.orqaga)
    staff = svc.staff()

    if a.xodimlar or not a.oy:
        for s in staff:
            print(f"{s.get('doctor_id'):>10}  {s.get('role', ''):<10} {staff_name(s)}")
        return 0

    target = None
    if a.shifokor:
        q = a.shifokor.lower()
        found = [s for s in staff if str(s.get("doctor_id")) == a.shifokor or q in staff_name(s).lower()]
        if len(found) != 1:
            print(f"Shifokor aniq topilmadi ({len(found)} ta mos): " +
                  ", ".join(staff_name(s) for s in found), file=sys.stderr)
            return 2
        target = str(found[0]["doctor_id"])
        if a.foiz is not None:
            orig = svc.percents.load
            svc.percents.load = lambda: ({**orig()[0], target: a.foiz}, orig()[1])

    print(f"Ma'lumot yuklanmoqda ({a.orqaga} oy orqaga, daqiqasiga 60 so'rov limiti bilan)...", file=sys.stderr)
    rep = svc.month_report(a.oy)
    doctors = [rep.doctor(target)] if target else rep.doctors
    for d in doctors:
        if d is None:
            print(f"{a.oy}: bu shifokorga pul tushmagan.")
            continue
        print(f"\n== {d.name} (ID {d.doctor_id}) — {a.oy}, foiz {d.percent}% ==")
        for r in d.rows:
            print(f"  {r.patient[:26]:<26} {r.work[:30]:<30} ish {r.done_date or '-':<10} "
                  f"tushgan {fmt(r.month_paid):>14}  ulush {fmt(r.share):>12}")
        print(f"  {'JAMI':<70} tushgan {fmt(d.total):>14}  maosh {fmt(d.salary):>12}")
    if not target:
        print(f"\nIshlar bo'yicha jami: {fmt(rep.total)}   Kassa jami: {fmt(rep.kassa_total)}   "
              f"Farq: {fmt(rep.difference)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
