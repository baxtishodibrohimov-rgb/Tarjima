"""Hisobotni Excel (.xlsx) faylga chiqarish."""
from __future__ import annotations

import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from report import DoctorReport, MonthReport

HEAD = Font(bold=True, color="FFFFFF")
FILL = PatternFill("solid", fgColor="2F5D8A")
MONEY = '#,##0.00'


def _header(ws, cols):
    ws.append(cols)
    for c in ws[ws.max_row]:
        c.font, c.fill, c.alignment = HEAD, FILL, Alignment(horizontal="center", wrap_text=True)


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _doctor_sheet(wb, d: DoctorReport, oy: str):
    title = re.sub(r"[\[\]:*?/\\]", "-", d.name)[:28] or d.doctor_id
    ws = wb.create_sheet(title)
    ws.append([f"{d.name} — {oy}: shu oy tushgan pul bo'yicha hisobot"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    _header(ws, ["Bemor", "Ish", "Ish sanasi", "Narx", "Jami to'langan",
                 "Shu oy tushgan", "To'lov sanalari", f"Ulush ({d.percent}%)"])
    for r in d.rows:
        ws.append([r.patient, r.work, r.done_date, float(r.price), float(r.paid_total),
                   float(r.month_paid), ", ".join(r.payment_dates), float(r.share)])
    ws.append(["JAMI", "", "", "", "", float(d.total), "", float(d.salary)])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=4, min_col=4, max_col=8):
        for c in row:
            if isinstance(c.value, float):
                c.number_format = MONEY
    _widths(ws, [28, 34, 12, 14, 14, 14, 22, 14])
    ws.freeze_panes = "A4"


def report_xlsx(rep: MonthReport, doctor_id: str | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Jami"
    if doctor_id is None:
        ws.append([f"Barcha shifokorlar — {rep.oy}"])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append([])
        _header(ws, ["Shifokor", "Bemorlar", "Shu oy tushgan", "Foiz", "Maosh"])
        for d in rep.doctors:
            ws.append([d.name, d.patients, float(d.total), float(d.percent), float(d.salary)])
        ws.append(["JAMI", "", float(rep.total), "", float(rep.salary)])
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
        if rep.kassa_total is not None:
            ws.append([])
            ws.append(["Kassa bo'yicha jami (GET /payments)", "", float(rep.kassa_total)])
            ws.append(["Farq (ishga bog'lanmagan to'lovlar)", "", float(rep.difference)])
        for row in ws.iter_rows(min_row=4, min_col=3, max_col=5):
            for c in row:
                if isinstance(c.value, float) and c.column != 4:
                    c.number_format = MONEY
        _widths(ws, [34, 10, 16, 8, 16])
        doctors = rep.doctors
    else:
        wb.remove(ws)
        doctors = [d for d in rep.doctors if d.doctor_id == str(doctor_id)]
    for d in doctors:
        _doctor_sheet(wb, d, rep.oy)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
