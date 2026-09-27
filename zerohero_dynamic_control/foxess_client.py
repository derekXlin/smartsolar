"""Signed transport for the FoxESS Cloud OpenAPI.

Auth
----
Every request carries ``token``, ``timestamp`` and ``signature`` headers, where

    signature = md5( path + r"\r\n" + token + r"\r\n" + timestamp )

THE GOTCHA: those separators are the *literal four characters* backslash-r-backslash-n,
NOT an actual carriage return and line feed. Writing "\r\n" in most languages produces
real control characters and the signature silently fails with an auth error that gives
no hint why. This is the single most common reason FoxESS API integrations do not work.
See ``_signature`` below — it uses a raw string deliberately.

Rate limits (from the official docs)
------------------------------------
    * 1440 interface calls per day, per inverter, per account
    * query endpoints:  1 request per second
    * update endpoints: 1 request per 2 seconds

1440/day is the binding constraint and it shapes the whole integration. A naive
60-second poll running 24/7 needs 1440 calls and would consume the entire allowance,
leaving nothing for control. This client therefore tracks the daily budget itself and
refuses to spend below a reserve, so a runaway loop degrades into stale telemetry
rather than locking the account out of its own inverter for the rest of the day.

Our actual budget, with the scheduler only active during the two windows:

    free charge 11:00-14:00 @ 60 s   180 reads
    credit window 18:00-21:00 @ 60 s 180 reads
    control writes (deadbanded)      ~40
    baseline save/restore            2
    ------------------------------------------
    total                            ~400 of 1440
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

BASE_URL = "https://www.foxesscloud.com"
USER_AGENT = "zerohero-dynamic-control/1.0"


class FoxESSError(RuntimeError):
    """Any failure talking to the FoxESS cloud."""


class FoxESSQuotaExhausted(FoxESSError):
    """The daily 1440-call budget is spent (or down to the reserve)."""


@dataclass
class CallBudget:
    """Tracks the 1440-calls-per-day-per-inverter limit."""

    daily_limit: int = 1440
    reserve: int = 120
    """Never spend the last N calls on routine polling — they are kept so the
    controller can always issue a close-out and restore the baseline schedule."""

    day: date | None = None
    used: int = 0
    by_path: dict[str, int] = field(default_factory=dict)

    def _roll(self, now: datetime) -> None:
        if self.day != now.date():
            if self.day is not None:
                log.info("FoxESS call budget reset (used %d yesterday)", self.used)
            self.day = now.date()
            self.used = 0
            self.by_path = {}

    def remaining(self, now: datetime) -> int:
        self._roll(now)
        return max(0, self.daily_limit - self.used)

    def check(self, now: datetime, *, path: str, critical: bool) -> None:
        self._roll(now)
        floor = 0 if critical else self.reserve
        if self.daily_limit - self.used <= floor:
            raise FoxESSQuotaExhausted(
                f"FoxESS daily budget spent ({self.used}/{self.daily_limit}); "
                f"refusing non-critical call to {path}"
            )

    def spend(self, path: str) -> None:
        self.used += 1
        self.by_path[path] = self.by_path.get(path, 0) + 1


class FoxESSClient:
    """Minimal signed client. Uses httpx when available, else urllib in a thread.

    Deliberately dependency-free by default: the controller must work on a Raspberry
    Pi with nothing but the standard library installed, because a missing wheel at
    17:50 costs a dollar.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timezone: str = "Australia/Sydney",
        timeout: float = 30.0,
        query_interval: float = 1.0,
        update_interval: float = 2.0,
        budget: CallBudget | None = None,
    ) -> None:
        if not api_key:
            raise FoxESSError("a FoxESS OpenAPI key is required (Cloud portal -> User Profile -> API Management)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timezone = timezone
        self.timeout = timeout
        self.query_interval = query_interval
        self.update_interval = update_interval
        self.budget = budget or CallBudget()
        self._last_call: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._httpx_client: Any = None

    # ------------------------------------------------------------------ signing
    @staticmethod
    def _signature(path: str, token: str, timestamp_ms: str) -> str:
        # The separators are LITERAL backslash-r-backslash-n. The raw string below
        # is load-bearing: swapping it for a normal string emits real CR/LF bytes
        # and every request comes back unauthorised.
        raw = rf"{path}\r\n{token}\r\n{timestamp_ms}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()  # noqa: S324 - vendor-specified

    def _headers(self, path: str) -> dict[str, str]:
        # Sign the BARE path: FoxESS excludes the query string from the signature.
        # Signing "/op/v0/device/battery/soc/get?sn=..." returns errno 40256
        # "illegal signature", which reads like an auth problem rather than the
        # off-by-one-query-string problem it actually is.
        sign_path = path.split("?", 1)[0]
        ts = str(int(time.time() * 1000))
        return {
            "token": self.api_key,
            "timestamp": ts,
            "signature": self._signature(sign_path, self.api_key, ts),
            "lang": "en",
            "timezone": self.timezone,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ throttle
    async def _throttle(self, path: str, minimum: float) -> None:
        last = self._last_call.get(path)
        if last is not None:
            wait = minimum - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_call[path] = time.monotonic()

    # --------------------------------------------------------------------- send
    async def request(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        critical: bool = False,
        now: datetime | None = None,
    ) -> Any:
        """Issue one signed call and return the ``result`` field.

        ``critical=True`` allows the call to draw on the reserved budget — used for
        close-out and baseline restore, which must always be able to run.
        """
        now = now or datetime.now()
        self.budget.check(now, path=path, critical=critical)

        is_update = "/set" in path or "/enable" in path or "/disable" in path
        async with self._lock:
            await self._throttle(path, self.update_interval if is_update else self.query_interval)
            payload = await self._send(path, body, method)
            self.budget.spend(path)

        errno = payload.get("errno", 0)
        if errno not in (0, None):
            raise FoxESSError(f"{path} returned errno={errno}: {payload.get('msg') or payload}")
        return payload.get("result")

    async def _send(self, path: str, body: dict[str, Any] | None, method: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._headers(path)
        data = json.dumps(body).encode("utf-8") if body is not None else None

        if self._httpx_client is None:
            try:
                import httpx

                self._httpx_client = httpx.AsyncClient(timeout=self.timeout)
            except ImportError:
                self._httpx_client = False  # marker: fall through to urllib

        if self._httpx_client:
            try:
                resp = await self._httpx_client.request(method, url, content=data, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                raise FoxESSError(f"{method} {path} failed: {exc}") from exc

        # Standard-library path so the app runs with no third-party HTTP client.
        def _blocking() -> dict[str, Any]:
            req = Request(url, data=data, headers=headers, method=method)
            with urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed https host
                return json.loads(resp.read().decode("utf-8"))

        try:
            return await asyncio.to_thread(_blocking)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise FoxESSError(f"{method} {path} failed: {exc}") from exc

    # ------------------------------------------------------------------ helpers
    async def device_list(self, page_size: int = 10) -> list[dict[str, Any]]:
        result = await self.request("/op/v0/device/list", {"currentPage": 1, "pageSize": page_size})
        if isinstance(result, dict):
            return result.get("data", []) or []
        return result or []

    async def real_query(self, sn: str, variables: list[str]) -> dict[str, float]:
        """Return {variable: value} for one inverter."""
        result = await self.request("/op/v0/device/real/query", {"sn": sn, "variables": variables})
        rows = result or []
        if rows and isinstance(rows[0], dict) and "datas" in rows[0]:
            rows = rows[0]["datas"]
        out: dict[str, float] = {}
        for row in rows:
            name = row.get("variable")
            value = row.get("value")
            if name is None:
                continue
            try:
                out[name] = float(value)
            except (TypeError, ValueError):
                log.debug("FoxESS variable %s had non-numeric value %r", name, value)
        return out

    async def get_min_soc(self, sn: str) -> dict[str, Any]:
        return await self.request(f"/op/v0/device/battery/soc/get?sn={sn}", method="GET")

    async def set_min_soc(self, sn: str, *, min_soc: int, min_soc_on_grid: int) -> None:
        await self.request(
            "/op/v0/device/battery/soc/set",
            {"sn": sn, "minSoc": int(min_soc), "minSocOnGrid": int(min_soc_on_grid)},
            critical=True,
        )

    async def scheduler_get(self, sn: str) -> dict[str, Any]:
        """Read the live schedule.

        Verified against an H3-10.0-Smart: /op/v0/device/scheduler/get returns an
        EMPTY group list even when groups exist, while /op/v1 returns them. v0 is
        kept as a fallback for older firmware, but v1 is tried first — silently
        believing "you have no schedule" would make us wipe a working setup.
        """
        for path in ("/op/v1/device/scheduler/get", "/op/v0/device/scheduler/get"):
            try:
                result = await self.request(path, {"deviceSN": sn}) or {}
            except FoxESSError as exc:
                log.debug("%s unavailable: %s", path, exc)
                continue
            if result.get("groups"):
                return result
        return {}

    async def scheduler_enable(self, sn: str, groups: list[dict[str, Any]], *, critical: bool = False) -> None:
        """Write the schedule, preferring the API version this firmware answers.

        /op/v0/device/scheduler/get returns an empty list on the H3-10.0-Smart
        while /op/v1 returns the real groups, so v0 is clearly not the version
        this firmware speaks. Try v1 first and keep v0 as the fallback for older
        units, rather than assuming either.
        """
        last: FoxESSError | None = None
        for path in ("/op/v1/device/scheduler/enable", "/op/v0/device/scheduler/enable"):
            try:
                await self.request(path, {"deviceSN": sn, "groups": groups}, critical=critical)
                return
            except FoxESSError as exc:
                log.debug("%s rejected the write: %s", path, exc)
                last = exc
        assert last is not None
        raise last

    async def scheduler_disable(self, sn: str, *, critical: bool = True) -> None:
        await self.request("/op/v0/device/scheduler/set/flag", {"deviceSN": sn, "enable": 0}, critical=critical)

    async def aclose(self) -> None:
        if self._httpx_client:
            await self._httpx_client.aclose()
