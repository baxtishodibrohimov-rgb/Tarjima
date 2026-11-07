"""Oylik hisobot mantig'i.

Asosiy qoida: shifokor maoshi **shu oyda kassaga tushgan pul** bo'yicha hisoblanadi,
ish qachon qilinganidan qat'i nazar.

1. Ish sanasi oralig'ini keng olamiz (masalan, 24 oy orqaga) va `DONE` hamda
   `INVOICED` holatidagi barcha ishlarni yig'amiz.
2. Har bir ishning `payments` ro'yxatidan sanasi tanlangan oyga tushganlarini qo'shamiz.
3. `done_by_doctor_id` bo'yicha guruhlaymiz va shifokor foizini qo'llaymiz.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

DONE_STATUSES = ("DONE", "INVOICED")
NO_DOCTOR = "0"


def money(value) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)


def parse_month(oy: str) -> tuple[date, date]:
    """'2026-11' -> (2026-11-01, 2026-11-30)."""
    y, m = (int(x) for x in oy.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def shift_months(d: date, months: int) -> date:
    idx = d.year * 12 + (d.month - 1) + months
    return date(idx // 12, idx % 12 + 1, 1)


def month_ranges(start: date, end: date, step: int = 3) -> list[tuple[date, date]]:
    """[start, end] oralig'ini `step` oylik bo'laklarga ajratadi (API so'rovlari uchun)."""
    out = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = shift_months(cur, step)
        last = min(end, date.fromordinal(nxt.toordinal() - 1))
        out.append((max(cur, start), last))
        cur = nxt
    return out


def staff_name(s: dict) -> str:
    return " ".join(p for p in (s.get("lastname"), s.get("firstname")) if p).strip() or f"#{s.get('doctor_id')}"


def patient_name(p: dict) -> str:
    return " ".join(x for x in (p.get("lastname"), p.get("firstname")) if x).strip() or f"#{p.get('patient_id')}"


@dataclass
class Row:
    plan_item_id: str
    patient_id: str
    patient: str
    work: str
    done_date: str
    price: Decimal
    paid_total: Decimal
    month_paid: Decimal
    payment_dates: list[str]
    share: Decimal = Decimal(0)


@dataclass
class DoctorReport:
    doctor_id: str
    name: str
    percent: Decimal
    rows: list[Row] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((r.month_paid for r in self.rows), Decimal(0))

    @property
    def salary(self) -> Decimal:
        return sum((r.share for r in self.rows), Decimal(0))

    @property
    def patients(self) -> int:
        return len({r.patient_id for r in self.rows})


@dataclass
class MonthReport:
    oy: str
    doctors: list[DoctorReport]
    kassa_total: Decimal | None = None  # GET /payments bo'yicha jami (tekshiruv uchun)

    @property
    def total(self) -> Decimal:
        return sum((d.total for d in self.doctors), Decimal(0))

    @property
    def salary(self) -> Decimal:
        return sum((d.salary for d in self.doctors), Decimal(0))

    @property
    def difference(self) -> Decimal | None:
        return None if self.kassa_total is None else self.kassa_total - self.total

    def doctor(self, doctor_id: str) -> DoctorReport | None:
        return next((d for d in self.doctors if d.doctor_id == str(doctor_id)), None)


def dedupe_items(items: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for it in items:
        seen[str(it.get("plan_item_id"))] = it
    return list(seen.values())


def build_report(items: list[dict], oy: str, staff: list[dict],
                 percents: dict[str, float], patients: dict[str, str],
                 default_percent: float = 0) -> MonthReport:
    staff_by_id = {str(s.get("doctor_id")): s for s in staff}
    doctors: dict[str, DoctorReport] = {}

    for it in dedupe_items(items):
        month_payments = [p for p in (it.get("payments") or [])
                          if str(p.get("date") or "")[:7] == oy]
        month_paid = sum((money(p.get("amount")) for p in month_payments), Decimal(0))
        if month_paid == 0:
            continue
        doc_id = str(it.get("done_by_doctor_id") or NO_DOCTOR)
        if doc_id not in doctors:
            if doc_id == NO_DOCTOR:
                name = "Shifokor belgilanmagan"
            elif doc_id in staff_by_id:
                name = staff_name(staff_by_id[doc_id])
            else:
                name = it.get("done_by") or f"#{doc_id}"
            pct = Decimal(str(percents.get(doc_id, default_percent))) if doc_id != NO_DOCTOR else Decimal(0)
            doctors[doc_id] = DoctorReport(doc_id, name, pct)
        d = doctors[doc_id]
        pid = str(it.get("patient_id") or "")
        row = Row(
            plan_item_id=str(it.get("plan_item_id")),
            patient_id=pid,
            patient=patients.get(pid, f"#{pid}"),
            work=it.get("name") or "",
            done_date=str(it.get("marked_done_date") or "")[:10],
            price=money(it.get("price_with_discount")),
            paid_total=money(it.get("paid")),
            month_paid=month_paid,
            payment_dates=sorted(str(p.get("date"))[:10] for p in month_payments),
        )
        row.share = (row.month_paid * d.percent / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
        d.rows.append(row)

    for d in doctors.values():
        d.rows.sort(key=lambda r: (r.patient.lower(), r.done_date))
    ordered = sorted(doctors.values(), key=lambda d: (d.doctor_id == NO_DOCTOR, -d.total))
    return MonthReport(oy=oy, doctors=ordered)


@dataclass
class Debt:
    patient_id: str
    patient: str
    work: str
    done_date: str
    price: Decimal
    paid: Decimal

    @property
    def left(self) -> Decimal:
        return self.price - self.paid


def build_debts(items: list[dict], staff: list[dict], patients: dict[str, str]) -> list[tuple[str, str, list[Debt]]]:
    """Shifokor bo'yicha to'lanmagan qoldiqlar: [(doctor_id, ism, [Debt...]), ...]."""
    staff_by_id = {str(s.get("doctor_id")): s for s in staff}
    groups: dict[str, list[Debt]] = {}
    names: dict[str, str] = {}
    for it in dedupe_items(items):
        price, paid = money(it.get("price_with_discount")), money(it.get("paid"))
        if price - paid <= 0:
            continue
        doc_id = str(it.get("done_by_doctor_id") or NO_DOCTOR)
        names.setdefault(doc_id, staff_name(staff_by_id[doc_id]) if doc_id in staff_by_id
                         else ("Shifokor belgilanmagan" if doc_id == NO_DOCTOR else it.get("done_by") or f"#{doc_id}"))
        pid = str(it.get("patient_id") or "")
        groups.setdefault(doc_id, []).append(Debt(pid, patients.get(pid, f"#{pid}"), it.get("name") or "",
                                                  str(it.get("marked_done_date") or "")[:10], price, paid))
    out = []
    for doc_id, debts in groups.items():
        debts.sort(key=lambda x: -x.left)
        out.append((doc_id, names[doc_id], debts))
    out.sort(key=lambda g: -sum(d.left for d in g[2]))
    return out
