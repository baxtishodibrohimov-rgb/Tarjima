"""Cliniccards API bilan ishlovchi kichik mijoz.

- Har bir so'rovda `Token` va `Content-Type` sarlavhalari yuboriladi.
- Daqiqasiga 60 ta so'rov limiti: xavfsizlik uchun 55 tadan oshirmaymiz.
- Javob `{"data", "result", "error"}` ko'rinishida; `result != "success"` bo'lsa xato.
"""
from __future__ import annotations

import collections
import threading
import time
from datetime import date

import httpx

BASE_URL = "https://cliniccards.com/api"


class CliniccardsError(RuntimeError):
    pass


class RateLimiter:
    """Sirpanuvchi oyna: `period` soniya ichida ko'pi bilan `limit` ta so'rov."""

    def __init__(self, limit: int = 55, period: float = 60.0):
        self.limit = limit
        self.period = period
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                time.sleep(self.period - (now - self._calls[0]) + 0.05)


class CliniccardsClient:
    def __init__(self, token: str, base_url: str = BASE_URL, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None,
                 limiter: RateLimiter | None = None):
        if not token:
            raise CliniccardsError("CLINICCARDS_TOKEN berilmagan (.env faylni tekshiring)")
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Token": token, "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._limiter = limiter or RateLimiter()

    def close(self) -> None:
        self._http.close()

    # --- past daraja ---------------------------------------------------
    def get(self, path: str, params: dict | None = None):
        last_error: Exception | None = None
        for attempt in range(4):
            self._limiter.wait()
            try:
                resp = self._http.get(path, params=params)
            except httpx.HTTPError as e:  # tarmoq xatosi
                last_error = e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = CliniccardsError(f"{path}: HTTP {resp.status_code}")
                time.sleep(2 ** attempt * 5 if resp.status_code == 429 else 2 ** attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                raise CliniccardsError(f"{path}: JSON emas javob (HTTP {resp.status_code})")
            if resp.status_code >= 400 or body.get("result") != "success":
                raise CliniccardsError(f"{path}: {body.get('error') or resp.status_code}")
            return body.get("data")
        raise CliniccardsError(f"{path}: so'rov bajarilmadi ({last_error})")

    # --- yuqori daraja -------------------------------------------------
    def staff(self) -> list[dict]:
        return self.get("/staff") or []

    def plan_items(self, status: str, start: date, end: date) -> list[dict]:
        return self.get(f"/planItems/{status}",
                        {"from": start.isoformat(), "to": end.isoformat()}) or []

    def payments(self, start: date, end: date) -> list[dict]:
        return self.get("/payments", {"from": start.isoformat(), "to": end.isoformat()}) or []

    def patients_by_ids(self, ids: list[str], batch: int = 50) -> list[dict]:
        out: list[dict] = []
        ids = [str(i) for i in ids if i]
        for i in range(0, len(ids), batch):
            chunk = ",".join(ids[i:i + batch])
            out.extend(self.get(f"/patients/{chunk}") or [])
        return out
