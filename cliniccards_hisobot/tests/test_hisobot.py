"""Soxta Cliniccards API bilan sinovlar (haqiqiy kalit kerak emas)."""
import base64
import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client import CliniccardsClient, CliniccardsError, RateLimiter  # noqa: E402
from report import build_report, month_ranges, parse_month  # noqa: E402
from service import Percents, Service  # noqa: E402
from datetime import date  # noqa: E402

STAFF = [
    {"doctor_id": "501", "lastname": "Karimova", "firstname": "madina", "role": "DOCTOR"},
    {"doctor_id": "502", "lastname": "Aliyev", "firstname": "Bobur", "role": "DOCTOR"},
]
ITEMS = {
    "DONE": [],
    "INVOICED": [
        # madina: oktyabrda qilingan ish, to'lov noyabrda (xulosadagi tekshiruv)
        {"plan_item_id": "1", "patient_id": "900", "name": "Plomba", "price_with_discount": "500000",
         "done_by_doctor_id": "501", "marked_done_date": "2026-10-20 00:00:00", "paid": "500000",
         "payments": [{"date": "2026-11-03 10:00:00", "amount": 500000, "payment_id": 1}]},
        # Bobur: martda breket, har oy to'lov; noyabrdagisi hisobga kiradi
        {"plan_item_id": "2", "patient_id": "901", "name": "Breket", "price_with_discount": "10000000",
         "done_by_doctor_id": "502", "marked_done_date": "2026-03-10 00:00:00", "paid": "9000000",
         "payments": [{"date": "2026-10-05 12:00:00", "amount": "1000000", "payment_id": 2},
                      {"date": "2026-11-05 12:00:00", "amount": "1000000", "payment_id": 3}]},
        # Shifokor belgilanmagan ish
        {"plan_item_id": "3", "patient_id": "900", "name": "Rentgen", "price_with_discount": "50000",
         "done_by_doctor_id": None, "marked_done_date": "2026-11-01 00:00:00", "paid": "50000",
         "payments": [{"date": "2026-11-01 09:00:00", "amount": 50000, "payment_id": 4}]},
        # pul tushmagan ish — hisobotda chiqmasligi kerak
        {"plan_item_id": "4", "patient_id": "901", "name": "Konsultatsiya", "price_with_discount": "100000",
         "done_by_doctor_id": "501", "marked_done_date": "2026-11-02 00:00:00", "paid": "0", "payments": None},
    ],
}
PATIENTS = {"900": {"patient_id": "900", "lastname": "Usmonov", "firstname": "Akmal"},
            "901": {"patient_id": "901", "lastname": "Rasulova", "firstname": "Dilnoza"}}


def fake_api(request: httpx.Request) -> httpx.Response:
    assert request.headers["Token"] == "test-token"
    path = request.url.path.removeprefix("/api")
    ok = lambda data: httpx.Response(200, json={"data": data, "result": "success", "error": None})  # noqa: E731
    if path == "/staff":
        return ok(STAFF)
    if path.startswith("/planItems/"):
        status = path.split("/")[2]
        # har bir bo'lakda faqat bir marta qaytaramiz: so'rov oralig'i 2026-10..2026-12 ga tushsa
        frm = request.url.params["from"]
        return ok(ITEMS[status] if frm <= "2026-10-01" <= request.url.params["to"] else [])
    if path.startswith("/patients/"):
        return ok([PATIENTS[i] for i in path.split("/")[2].split(",") if i in PATIENTS])
    if path == "/payments":
        return ok([{"amount": "500000"}, {"amount": "1000000"}, {"amount": "50000"}, {"amount": "200000"}])
    return httpx.Response(404, json={"data": None, "result": "fail", "error": "not found"})


@pytest.fixture
def svc(tmp_path):
    client = CliniccardsClient("test-token", base_url="https://cliniccards.test/api",
                               transport=httpx.MockTransport(fake_api), limiter=RateLimiter(1000, 1))
    p = Percents(tmp_path / "foizlar.json")
    p.save({"501": 25, "502": 30})
    return Service(client, p, months_back=24)


def test_madina_november(svc):
    rep = svc.month_report("2026-11")
    m = rep.doctor("501")
    assert m.total == Decimal("500000")
    assert m.salary == Decimal("125000")
    assert [r.work for r in m.rows] == ["Plomba"]  # pul tushmagan konsultatsiya chiqmaydi
    assert m.rows[0].patient == "Usmonov Akmal"


def test_october_has_no_madina_salary(svc):
    rep = svc.month_report("2026-10")
    assert rep.doctor("501") is None
    assert rep.doctor("502").total == Decimal("1000000")


def test_installments_and_unassigned(svc):
    rep = svc.month_report("2026-11")
    assert rep.doctor("502").total == Decimal("1000000")
    assert rep.doctor("502").salary == Decimal("300000")
    assert rep.doctor("0").total == Decimal("50000")
    assert rep.doctor("0").salary == 0
    assert rep.doctors[-1].doctor_id == "0"
    assert rep.total == Decimal("1550000")
    assert rep.kassa_total == Decimal("1750000")
    assert rep.difference == Decimal("200000")


def test_debts(svc):
    groups = svc.debts("2026-11")
    left = {doc: sum(d.left for d in items) for doc, _, items in groups}
    assert left == {"502": Decimal("1000000"), "501": Decimal("100000")}


def test_duplicates_counted_once():
    item = ITEMS["INVOICED"][0]
    rep = build_report([item, dict(item)], "2026-11", STAFF, {"501": 25}, {})
    assert rep.doctor("501").total == Decimal("500000")


def test_month_ranges_cover_period():
    start, end = date(2024, 11, 1), date(2026, 11, 30)
    ranges = month_ranges(start, end)
    assert ranges[0][0] == start and ranges[-1][1] == end
    for (a, b), (c, _) in zip(ranges, ranges[1:]):
        assert (c - b).days == 1
    assert parse_month("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))


def test_api_fail_raises():
    client = CliniccardsClient("test-token", base_url="https://cliniccards.test/api",
                               transport=httpx.MockTransport(fake_api), limiter=RateLimiter(1000, 1))
    with pytest.raises(CliniccardsError):
        client.get("/nomalum")


def test_excel(svc):
    from excel import report_xlsx
    from openpyxl import load_workbook
    import io
    wb = load_workbook(io.BytesIO(report_xlsx(svc.month_report("2026-11"))))
    assert wb.sheetnames[0] == "Jami" and len(wb.sheetnames) == 4


def test_web(svc, tmp_path, monkeypatch):
    monkeypatch.setenv("HISOBOT_PAROL", "maxfiy")
    import app as webapp
    from fastapi.testclient import TestClient
    monkeypatch.setattr(webapp, "_service", svc)
    c = TestClient(webapp.app)
    assert c.get("/").status_code == 401
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:maxfiy").decode()}
    r = c.get("/?oy=2026-11", headers=auth)
    assert r.status_code == 200 and "125 000" in r.text
    r = c.get("/shifokor/501?oy=2026-11", headers=auth)
    assert "Usmonov Akmal" in r.text and "Konsultatsiya" not in r.text
    assert c.get("/excel?oy=2026-11&shifokor=501", headers=auth).content[:2] == b"PK"
    assert c.get("/qarzdorlar", headers=auth).status_code == 200
    r = c.post("/sozlamalar", data={"foiz_501": "20", "default": "0"}, headers=auth, follow_redirects=False)
    assert r.status_code == 303
    assert json.loads((tmp_path / "foizlar.json").read_text())["doctors"] == {"501": 20.0}
    assert c.get("/?oy=2026-13", headers=auth).status_code == 400
