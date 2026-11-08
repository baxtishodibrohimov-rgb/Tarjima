"""API'dan ma'lumot yig'ish, keshlash va foizlar sozlamasi."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path

from client import CliniccardsClient
from report import (DONE_STATUSES, MonthReport, build_debts, build_report, money,
                    month_ranges, parse_month, patient_name, shift_months)

HERE = Path(__file__).resolve().parent


def load_env(path: Path = HERE / ".env") -> None:
    """Oddiy .env o'quvchi (KEY=VALUE), tashqi kutubxonasiz."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Percents:
    """Shifokor foizlari: foizlar.json -> {"default": 0, "doctors": {"<doctor_id>": 25}}."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> tuple[dict[str, float], float]:
        if not self.path.exists():
            return {}, 0.0
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (data.get("doctors") or {}).items()}, float(data.get("default") or 0)

    def save(self, doctors: dict[str, float], default: float = 0.0) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"default": default, "doctors": doctors}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)


class Service:
    def __init__(self, client: CliniccardsClient, percents: Percents,
                 months_back: int = 24, cache_ttl: int = 900):
        self.client = client
        self.percents = percents
        self.months_back = months_back
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    # --- kesh ----------------------------------------------------------
    def _cached(self, key: str, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        return value

    def clear_cache(self) -> None:
        self._cache.clear()

    # --- ma'lumot -------------------------------------------------------
    def staff(self) -> list[dict]:
        return self._cached("staff", self.client.staff)

    def items(self, oy: str) -> list[dict]:
        """Tanlangan oydan `months_back` oy oldingi ishlardan bugungacha."""
        start = shift_months(parse_month(oy)[0], -self.months_back)
        end = max(date.today(), parse_month(oy)[1])

        def fetch():
            out: list[dict] = []
            for a, b in month_ranges(start, end):
                for status in DONE_STATUSES:
                    out.extend(self.client.plan_items(status, a, b))
            return out

        with self._lock:
            return self._cached(f"items:{start}:{end}", fetch)

    def patient_names(self, ids) -> dict[str, str]:
        names: dict[str, str] = self._cache.setdefault("patients", (float("inf"), {}))[1]
        missing = sorted({str(i) for i in ids if i} - names.keys())
        for p in self.client.patients_by_ids(missing):
            names[str(p.get("patient_id"))] = patient_name(p)
        return names

    def kassa_total(self, oy: str):
        a, b = parse_month(oy)
        return self._cached(f"payments:{oy}",
                            lambda: sum((money(p.get("amount")) for p in self.client.payments(a, b)), money(0)))

    # --- hisobotlar ------------------------------------------------------
    def month_report(self, oy: str, with_kassa: bool = True) -> MonthReport:
        items = self.items(oy)
        relevant = [it for it in items if any(str(p.get("date") or "")[:7] == oy for p in (it.get("payments") or []))]
        names = self.patient_names(it.get("patient_id") for it in relevant)
        percents, default = self.percents.load()
        rep = build_report(relevant, oy, self.staff(), percents, names, default)
        if with_kassa:
            rep.kassa_total = self.kassa_total(oy)
        return rep

    def debts(self, oy: str):
        items = self.items(oy)
        unpaid = [it for it in items if money(it.get("price_with_discount")) > money(it.get("paid"))]
        names = self.patient_names(it.get("patient_id") for it in unpaid)
        return build_debts(unpaid, self.staff(), names)
