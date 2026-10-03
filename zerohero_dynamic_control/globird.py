"""Read GloBird's own daily cost breakdown from the customer portal.

The controller can only estimate import from five-minute cloud readings; whether
the ZEROHERO credit was paid is decided by GloBird's meter data. Until now that
truth arrived as screenshots of the portal's cost chart. This reads the same data
the chart is drawn from and records it with `Ledger.record_bill`.

There is no published API. The portal at myaccount.globirdenergy.com.au is a web
app over a JSON API, and the flow below follows the open-source globird-ha Home
Assistant integration (github.com/bolagnaise/globird-ha):

    GET  /                               session + Azure sticky-routing cookies
    GET  /api/account/publicjwk          RSA key for the password
    POST /api/account/login              {emailAddress, password: RSA-OAEP-SHA256, ...}
    GET  /api/account/currentuser        accounts -> services (accountServiceId, NMI)
    POST /api/transaction/CostDetail     one row per charge per day

Read-only: it never changes anything on the account. Credentials come only from
the environment (GLOBIRD_EMAIL, GLOBIRD_PASSWORD), never from config.yaml. The
session cookies are kept on disk so a daily fetch re-uses one login instead of
logging in every time, which is what tends to trigger the portal's captcha.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import BillRecord

log = logging.getLogger(__name__)

BASE_URL = "https://myaccount.globirdenergy.com.au"
ENV_EMAIL = "GLOBIRD_EMAIL"
ENV_PASSWORD = "GLOBIRD_PASSWORD"


class GloBirdError(RuntimeError):
    """The portal could not be read."""


class GloBirdAuthError(GloBirdError):
    """Login refused: wrong credentials, or the portal wants a captcha."""


def credentials_from_env() -> tuple[str, str] | None:
    email = (os.environ.get(ENV_EMAIL) or "").strip()
    password = os.environ.get(ENV_PASSWORD) or ""
    return (email, password) if email and password else None


def encrypt_password(password: str, jwk: dict[str, Any]) -> str:
    """RSA-OAEP with SHA-256, as the portal's login page does it."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    def _int(b64: str) -> int:
        return int.from_bytes(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)), "big")

    key = RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
    cipher = key.encrypt(
        password.encode("utf-8"),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(cipher).decode("ascii")


def bills_from_cost_rows(rows: list[dict[str, Any]], *, today: date) -> list[BillRecord]:
    """Group CostDetail rows into one BillRecord per finished day.

    A day counts only once it has more than the fixed SUPPLY row (the portal
    posts supply first, usage later), and only before ``today``. A ZEROHERO
    credit row with a non-zero amount means the credit was paid.
    """
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = str(row.get("date") or "").split("T")[0].replace("/", "-")
        if day:
            by_day[day].append(row)
    bills = []
    for day, day_rows in sorted(by_day.items()):
        try:
            if date.fromisoformat(day) >= today:
                continue
        except ValueError:
            continue
        categories: dict[str, float] = defaultdict(float)
        for row in day_rows:
            name = str(row.get("chargeCategory") or "").strip().lower()
            try:
                categories[name] += float(row.get("amount") or 0.0)
            except (TypeError, ValueError):
                continue
        if not any(name != "supply" for name in categories):
            continue                          # supply only: the day is not in yet

        def pick(*markers: str, cats: dict[str, float] = categories) -> float | None:
            hits = [v for k, v in cats.items() if any(m in k for m in markers)]
            return round(sum(hits), 2) if hits else None

        credit = pick("zerohero")
        bills.append(BillRecord(
            date=day,
            credit_paid=credit is not None and abs(credit) > 1e-9,
            total_cost_aud=round(sum(categories.values()), 2),
            usage_aud=pick("usage"),
            solar_aud=pick("solar"),
            super_export_topup_aud=pick("super export"),
            source="globird-portal",
        ))
    return bills


class GloBirdPortal:
    def __init__(self, email: str, password: str, *, session_path: Path | None = None,
                 base_url: str = BASE_URL, transport: Any = None, timeout: float = 30.0) -> None:
        import httpx

        self.email = email
        self.password = password
        self.session_path = Path(session_path) if session_path else None
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, transport=transport, follow_redirects=True,
            headers={"Accept": "application/json, text/plain, */*", "Origin": self.base_url,
                     "Referer": f"{self.base_url}/", "User-Agent": "zerohero-dynamic-control"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ session
    def _load_cookies(self) -> bool:
        if self.session_path is None or not self.session_path.exists():
            return False
        try:
            for c in json.loads(self.session_path.read_text()):
                self._http.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring unreadable GloBird session file: %s", exc)
            return False

    def _save_cookies(self) -> None:
        if self.session_path is None:
            return
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                   for c in self._http.cookies.jar]
        try:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            self.session_path.write_text(json.dumps(cookies))
            os.chmod(self.session_path, 0o600)        # it is a logged-in session
        except OSError as exc:
            log.warning("could not save the GloBird session (%s); next fetch logs in again", exc)

    async def _json(self, method: str, path: str, body: Any = None, *, allow_failure: bool = False) -> Any:
        import httpx

        try:
            resp = await self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise GloBirdError(f"GloBird portal unreachable: {exc!r}") from exc
        if resp.status_code in (401, 403):
            raise GloBirdAuthError(f"GloBird session not accepted (HTTP {resp.status_code})")
        if not 200 <= resp.status_code < 300:
            raise GloBirdError(f"GloBird portal returned HTTP {resp.status_code} for {path}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise GloBirdError(f"GloBird portal returned non-JSON for {path}") from exc
        if isinstance(payload, dict) and payload.get("success") is False and not allow_failure:
            raise GloBirdError(f"GloBird portal refused {path}: {payload.get('message') or 'no message'}")
        return payload

    async def _login(self) -> None:
        await self._http.get("/")
        # The Azure load balancer sets its sticky-routing cookies for the backend
        # host; copy them to the portal host so every request hits the same shard.
        host = self.base_url.split("://", 1)[-1]
        for c in list(self._http.cookies.jar):
            if "arraff" in c.name.lower() and c.domain.lstrip(".") != host:
                self._http.cookies.set(c.name, c.value, domain=host)
        jwk = await self._json("GET", "/api/account/publicjwk")
        payload = await self._json("POST", "/api/account/login", {
            "emailAddress": self.email, "password": encrypt_password(self.password, jwk),
            "rememberMe": True,
        }, allow_failure=True)
        data = (payload or {}).get("data") or {}
        if data.get("requireRetryCaptCha") or data.get("requireHCaptcha"):
            raise GloBirdAuthError(
                "GloBird asked for a captcha. Log in once at myaccount.globirdenergy.com.au "
                "in a browser; the next fetch usually goes through."
            )
        if not payload.get("success") or data.get("isLoginSucceeded") is False:
            raise GloBirdAuthError(f"GloBird login failed: {payload.get('message') or data.get('message') or 'no reason given'}")
        log.info("logged in to the GloBird portal")

    async def current_user(self) -> dict[str, Any]:
        """The account, re-using a saved session when it is still good."""
        if self._load_cookies():
            try:
                user = await self._json("GET", "/api/account/currentuser")
                if (user or {}).get("data"):
                    return user
            except GloBirdError:
                pass
            self._http.cookies.clear()
        await self._login()
        user = await self._json("GET", "/api/account/currentuser")
        self._save_cookies()
        return user

    @staticmethod
    def electricity_service(user: dict[str, Any]) -> dict[str, Any]:
        for account in ((user or {}).get("data") or {}).get("accounts") or []:
            for svc in account.get("services") or []:
                kind = str(svc.get("serviceType") or "").lower()
                if svc.get("closedDate") or str(svc.get("status") or "").lower() == "closed":
                    continue
                if ("power" in kind or "electric" in kind) and svc.get("accountServiceId") and svc.get("siteIdentifier"):
                    return svc
        raise GloBirdError("no open electricity service on this GloBird account")

    async def cost_rows(self, service: dict[str, Any], start: date, end: date) -> list[dict[str, Any]]:
        payload = await self._json("POST", "/api/transaction/CostDetail", {
            "accountServiceId": service["accountServiceId"], "identifier": str(service["siteIdentifier"]),
            "from": start.isoformat(), "to": end.isoformat(), "isSmart": True,
        })
        rows = (payload or {}).get("data")
        return rows if isinstance(rows, list) else []

    async def fetch_bills(self, *, days: int, today: date) -> list[BillRecord]:
        user = await self.current_user()
        service = self.electricity_service(user)
        rows = await self.cost_rows(service, today - timedelta(days=days), today)
        self._save_cookies()
        return bills_from_cost_rows(rows, today=today)


def new_or_changed(fetched: list[BillRecord], existing: dict[str, BillRecord]) -> list[BillRecord]:
    """Only bills that add something: a new day, or a day GloBird has since revised
    (a credit posted late, a corrected reading). Re-recording unchanged days would
    grow the ledger every fetch for nothing."""
    keys = ("credit_paid", "total_cost_aud", "usage_aud", "solar_aud", "super_export_topup_aud")
    out = []
    for bill in fetched:
        old = existing.get(bill.date)
        if old is None or any(getattr(old, k) != getattr(bill, k) for k in keys):
            out.append(bill)
    return out


async def fetch_and_record(ledger: Any, *, days: int, now: datetime, session_path: Path | None,
                           transport: Any = None) -> list[BillRecord]:
    """Fetch the last ``days`` days and record what is new or revised."""
    creds = credentials_from_env()
    if creds is None:
        raise GloBirdAuthError(f"set {ENV_EMAIL} and {ENV_PASSWORD} in the environment (.env on the NAS)")
    portal = GloBirdPortal(*creds, session_path=session_path, transport=transport)
    try:
        fetched = await portal.fetch_bills(days=days, today=now.date())
    finally:
        await portal.aclose()
    fresh = new_or_changed(fetched, ledger.read_bills())
    for bill in fresh:
        ledger.record_bill(bill.model_copy(update={"recorded_at": now}))
    return fresh
