"""Veb-sahifa: oylik hisobot, shifokor tafsiloti, qarzdorlar, foizlar sozlamasi.

Ishga tushirish:  uvicorn app:app --host 0.0.0.0 --port 8090
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from client import CliniccardsClient, CliniccardsError
from excel import report_xlsx
from service import HERE, Percents, Service, load_env

load_env()

security = HTTPBasic()
templates = Jinja2Templates(directory=str(HERE / "templates"))


def fmt(value) -> str:
    if value is None:
        return "—"
    v = Decimal(value).quantize(Decimal("0.01"))
    s = f"{v:,.2f}".replace(",", " ")
    return s[:-3] if s.endswith(".00") else s


templates.env.filters["pul"] = fmt


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def check_month(oy: str | None) -> str:
    oy = oy or current_month()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", oy):
        raise HTTPException(400, "Oy YYYY-MM ko'rinishida bo'lishi kerak")
    return oy


def make_service() -> Service:
    client = CliniccardsClient(os.environ.get("CLINICCARDS_TOKEN", ""))
    percents = Percents(Path(os.environ.get("FOIZLAR_FAYL", HERE / "foizlar.json")))
    return Service(client, percents, months_back=int(os.environ.get("ORQAGA_OY", "24")))


app = FastAPI(title="Cliniccards maosh hisoboti")
_service: Service | None = None


def service() -> Service:
    global _service
    if _service is None:
        _service = make_service()
    return _service


def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    user = os.environ.get("HISOBOT_LOGIN", "admin")
    password = os.environ.get("HISOBOT_PAROL", "")
    if not password:
        raise HTTPException(500, "HISOBOT_PAROL .env faylda berilmagan")
    ok = secrets.compare_digest(creds.username.encode(), user.encode()) and \
        secrets.compare_digest(creds.password.encode(), password.encode())
    if not ok:
        raise HTTPException(401, "Login yoki parol noto'g'ri", headers={"WWW-Authenticate": "Basic"})
    return creds.username


@app.exception_handler(CliniccardsError)
def api_error(request: Request, exc: CliniccardsError):
    return templates.TemplateResponse(request, "error.html", {"error": str(exc)}, status_code=502)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index(request: Request, oy: str | None = None, _=Depends(auth)):
    oy = check_month(oy)
    rep = service().month_report(oy)
    return templates.TemplateResponse(request, "index.html", {"rep": rep, "oy": oy})


@app.get("/shifokor/{doctor_id}")
def doctor(request: Request, doctor_id: str, oy: str | None = None, _=Depends(auth)):
    oy = check_month(oy)
    rep = service().month_report(oy, with_kassa=False)
    d = rep.doctor(doctor_id)
    if d is None:
        staff = {str(s.get("doctor_id")): s for s in service().staff()}
        if doctor_id not in staff:
            raise HTTPException(404, "Shifokor topilmadi")
    return templates.TemplateResponse(request, "doctor.html", {"d": d, "oy": oy, "doctor_id": doctor_id})


@app.get("/excel")
def excel(oy: str | None = None, shifokor: str | None = None, _=Depends(auth)):
    oy = check_month(oy)
    rep = service().month_report(oy, with_kassa=shifokor is None)
    data = report_xlsx(rep, shifokor)
    name = f"maosh_{oy}" + (f"_{shifokor}" if shifokor else "") + ".xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/qarzdorlar")
def debts(request: Request, _=Depends(auth)):
    groups = service().debts(current_month())
    return templates.TemplateResponse(request, "debts.html", {"groups": groups, "oy": current_month()})


@app.get("/sozlamalar")
def settings_page(request: Request, saved: int = 0, _=Depends(auth)):
    percents, default = service().percents.load()
    staff = sorted(service().staff(), key=lambda s: (s.get("role") != "DOCTOR", s.get("lastname") or ""))
    return templates.TemplateResponse(request, "settings.html", {
        "staff": staff, "percents": percents, "default": default, "saved": saved})


@app.post("/sozlamalar")
async def settings_save(request: Request, default: float = Form(0), _=Depends(auth)):
    form = await request.form()
    doctors: dict[str, float] = {}
    for key, value in form.items():
        if key.startswith("foiz_") and str(value).strip() != "":
            try:
                pct = float(str(value).replace(",", "."))
            except ValueError:
                raise HTTPException(400, f"Noto'g'ri foiz: {value}")
            if not 0 <= pct <= 100:
                raise HTTPException(400, "Foiz 0 dan 100 gacha bo'lishi kerak")
            doctors[key[5:]] = pct
    service().percents.save(doctors, default)
    return RedirectResponse("/sozlamalar?saved=1", status_code=303)


@app.post("/yangilash")
def refresh(request: Request, _=Depends(auth)):
    service().clear_cache()
    ref = urlsplit(request.headers.get("referer") or "")
    target = ref.path + (f"?{ref.query}" if ref.query else "") if ref.path.startswith("/") else "/"
    return RedirectResponse(target, status_code=303)
