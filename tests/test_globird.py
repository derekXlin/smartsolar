"""GloBird portal client, against a fake portal speaking the same JSON API."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime

import httpx
import pytest

# The portal login needs the optional `globird` extra. Without it, skip these tests
# rather than fail collection of the whole suite (which is what CI hit on 3 Oct).
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from zerohero_dynamic_control.globird import (
    GloBirdAuthError,
    GloBirdPortal,
    bills_from_cost_rows,
    fetch_and_record,
    new_or_changed,
)
from zerohero_dynamic_control.ledger import Ledger

from .conftest import TZ

TODAY = date(2026, 10, 3)


def _row(day, category, amount):
    return {"date": f"{day}T00:00:00", "amount": amount, "quantity": 1, "chargeCategory": category}


# The 2 Oct and 27 Sep screenshots, as the API would return them.
COST_ROWS = [
    _row("2026-09-27", "SUPPLY", 1.58), _row("2026-09-27", "USAGE", 0.19),
    _row("2026-09-27", "SOLAR", -0.07), _row("2026-09-27", "Super Export top up", -0.28),
    _row("2026-10-02", "SUPPLY", 1.58), _row("2026-10-02", "USAGE", 0.21),
    _row("2026-10-02", "ZEROHERO Credit", -1.0), _row("2026-10-02", "SOLAR", -0.06),
    _row("2026-10-02", "Super Export top up", -0.24),
    _row("2026-10-01", "SUPPLY", 1.58),                       # usage not posted yet
    _row("2026-10-03", "SUPPLY", 1.58), _row("2026-10-03", "USAGE", 0.10),   # today: unfinished
]


class FakePortal:
    def __init__(self, *, captcha=False):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.captcha = captcha
        self.logins = 0
        self.passwords: list[str] = []

    def _jwk(self):
        nums = self.key.public_key().public_numbers()
        def b64(i):
            return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()
        return {"kty": "RSA", "n": b64(nums.n), "e": b64(nums.e)}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        authed = "portal_session=ok" in request.headers.get("cookie", "")
        if path == "/":
            return httpx.Response(200, text="<html>", headers={"set-cookie": "ARRAffinity=shard1; Path=/"})
        if path == "/api/account/publicjwk":
            return httpx.Response(200, json=self._jwk())
        if path == "/api/account/login":
            self.logins += 1
            body = json.loads(request.content)
            self.passwords.append(self.key.decrypt(
                base64.b64decode(body["password"]),
                padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
            ).decode())
            if self.captcha:
                return httpx.Response(200, json={"success": False, "data": {"requireHCaptcha": True}})
            return httpx.Response(200, json={"success": True, "data": {"isLoginSucceeded": True}},
                                  headers={"set-cookie": "portal_session=ok; Path=/"})
        if not authed:
            return httpx.Response(401)
        if path == "/api/account/currentuser":
            return httpx.Response(200, json={"success": True, "data": {"accounts": [{"accountId": 1, "services": [
                {"serviceType": "Gas", "accountServiceId": 7, "siteIdentifier": "5512345678"},
                {"serviceType": "Power", "accountServiceId": 9, "siteIdentifier": "4000000001"},
            ]}]}})
        if path == "/api/transaction/CostDetail":
            body = json.loads(request.content)
            assert body["accountServiceId"] == 9 and body["identifier"] == "4000000001"
            return httpx.Response(200, json={"success": True, "data": COST_ROWS})
        return httpx.Response(404)


def test_rows_become_one_bill_per_finished_day():
    bills = {b.date: b for b in bills_from_cost_rows(COST_ROWS, today=TODAY)}
    assert set(bills) == {"2026-09-27", "2026-10-02"}, "supply-only and today's rows wait"
    assert not bills["2026-09-27"].credit_paid and bills["2026-09-27"].total_cost_aud == pytest.approx(1.42)
    oct2 = bills["2026-10-02"]
    assert oct2.credit_paid and oct2.total_cost_aud == pytest.approx(0.49)
    assert (oct2.usage_aud, oct2.solar_aud, oct2.super_export_topup_aud) == (0.21, -0.06, -0.24)
    assert oct2.source == "globird-portal"


@pytest.mark.asyncio
async def test_login_encrypts_the_password_and_reads_costs(tmp_path):
    fake = FakePortal()
    portal = GloBirdPortal("me@example.com", "s3cret!", session_path=tmp_path / "s.json",
                           base_url="https://portal.test", transport=httpx.MockTransport(fake.handler))
    bills = await portal.fetch_bills(days=7, today=TODAY)
    await portal.aclose()
    assert fake.passwords == ["s3cret!"], "the portal decrypted exactly the password we hold"
    assert [b.date for b in bills] == ["2026-09-27", "2026-10-02"]


@pytest.mark.asyncio
async def test_a_saved_session_is_reused_instead_of_logging_in_again(tmp_path):
    """Logging in every fetch is what tends to trigger the portal's captcha."""
    fake = FakePortal()
    for _ in range(3):
        portal = GloBirdPortal("me@example.com", "pw", session_path=tmp_path / "s.json",
                               base_url="https://portal.test", transport=httpx.MockTransport(fake.handler))
        await portal.fetch_bills(days=7, today=TODAY)
        await portal.aclose()
    assert fake.logins == 1


@pytest.mark.asyncio
async def test_captcha_is_a_clear_error_not_a_silent_empty_fetch(tmp_path):
    portal = GloBirdPortal("me@example.com", "pw", base_url="https://portal.test",
                           transport=httpx.MockTransport(FakePortal(captcha=True).handler))
    with pytest.raises(GloBirdAuthError, match="captcha"):
        await portal.fetch_bills(days=7, today=TODAY)
    await portal.aclose()


@pytest.mark.asyncio
async def test_fetch_records_new_and_revised_days_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GLOBIRD_EMAIL", "me@example.com")
    monkeypatch.setenv("GLOBIRD_PASSWORD", "pw")
    ledger = Ledger(tmp_path / "ledger.jsonl", tmp_path / "d.jsonl")
    now = datetime(2026, 10, 3, 7, 30, tzinfo=TZ)
    fake = FakePortal()

    async def fetch():
        return await fetch_and_record(ledger, days=7, now=now, session_path=tmp_path / "s.json",
                                      transport=httpx.MockTransport(fake.handler))

    # The mock transport answers for any host, so the real portal URL never leaves the test.
    first = await fetch()
    assert [b.date for b in first] == ["2026-09-27", "2026-10-02"]
    assert await fetch() == [], "nothing new: nothing recorded"
    COST_ROWS.append(_row("2026-10-01", "USAGE", 0.16))       # 1 Oct's usage lands later
    try:
        assert [b.date for b in await fetch()] == ["2026-10-01"]
    finally:
        COST_ROWS.pop()
    assert ledger.read_bills()["2026-10-02"].source == "globird-portal"


def test_a_manual_entry_with_the_same_figures_is_not_duplicated():
    from zerohero_dynamic_control.models import BillRecord

    manual = BillRecord(date="2026-10-02", credit_paid=True, total_cost_aud=0.49, usage_aud=0.21,
                        solar_aud=-0.06, super_export_topup_aud=-0.24)
    fetched = bills_from_cost_rows(COST_ROWS, today=TODAY)
    assert [b.date for b in new_or_changed(fetched, {"2026-10-02": manual})] == ["2026-09-27"]
