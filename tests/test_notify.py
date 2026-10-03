"""Daily ntfy messages: what they say, and when they go out."""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx
import pytest

from zerohero_dynamic_control.clock import SimClock
from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.models import BillRecord, DailyOutcome, HourImport
from zerohero_dynamic_control.notify import Ntfy, compose_evening, compose_morning

from .conftest import TZ

DECISION = {
    "made_at": "2026-10-02T17:50:03+10:00", "window_start": "2026-10-02T18:00:00+10:00",
    "starting_soc_pct": 89.0, "opportunistic_export_kwh": 2.8, "expected_final_soc": 67.1,
    "credit_achievable": True, "telemetry_assumed": False, "degraded": False,
    "rationale": ["load learned from up to 5 recent evenings: 18:00 2.0 kW, 19:00 2.0 kW, 20:00 2.2 kW",
                  "control: force-discharge 18:00-18:20 exporting 2.6 kWh, self-use for the rest of the window"],
}
BILL = BillRecord(date="2026-10-02", credit_paid=True, total_cost_aud=0.49, usage_aud=0.21,
                  solar_aud=-0.06, super_export_topup_aud=-0.24, source="globird-portal")


@pytest.mark.asyncio
async def test_ntfy_posts_json_so_titles_can_be_utf8():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    ok = await Ntfy("https://ntfy.example", "zerohero-secret", transport=httpx.MockTransport(handler)).send(
        "Tonight: sell 5.6 kWh — 18:00", "body", tags=["battery"])
    assert ok and seen == [{"topic": "zerohero-secret", "title": "Tonight: sell 5.6 kWh — 18:00",
                            "message": "body", "priority": 3, "tags": ["battery"]}]


@pytest.mark.asyncio
async def test_a_failed_send_returns_false_rather_than_raising():
    def handler(request):
        raise httpx.ConnectError("no route")

    assert not await Ntfy("https://ntfy.example", "t", transport=httpx.MockTransport(handler)).send("t", "b")


def test_morning_message_leads_with_the_bill():
    outcome = DailyOutcome(date="2026-10-02", exported_kwh=2.74, final_soc_pct=67.0, hourly_import=[
        HourImport(hour_start=datetime(2026, 10, 2, h, tzinfo=TZ), imported_kwh=w / 1000) for h, w in
        ((18, 0.5), (19, 20.0), (20, 10.0))])
    title, body, tags = compose_morning(
        date(2026, 10, 2), outcome=outcome, bill=BILL, decision=DECISION, free_window=None,
        overnight={"low_soc": 29.0, "low_at": "06:59", "import_kwh": 0.29, "until": "07:30"},
        month_bills=[BILL], topup_rate=0.08, health=["Controller 1.7.0 (abc123)"])
    assert title == "ZeroHero Fri 02 Oct: credit paid, day $0.49" and tags == ["white_check_mark"]
    assert "sold 3.0 kWh" in body
    assert "• Evening: battery 89% at 17:50\n    ◦ force-discharge 18:00-18:20 exporting 2.6 kWh\n    ◦ then self-use" in body
    assert "• Overnight: lowest 29% at 06:59\n    ◦ bought 0.29 kWh before 07:30" in body
    assert "October so far: credit 1/1 days, total $0.49" in body


def test_morning_message_without_the_bill_says_so():
    title, body, _ = compose_morning(
        date(2026, 10, 2), outcome=DailyOutcome(date="2026-10-02", credit_secured=True, credit_verified=True,
                                                 hourly_import=[]),
        bill=None, decision=DECISION, free_window=None, overnight=None, month_bills=[],
        topup_rate=0.08, health=[])
    assert title.endswith("(bill pending)") and "GloBird: not published yet" in body


def test_evening_message_states_the_plan_and_warns_when_the_credit_is_lost():
    title, body, tags = compose_evening(DECISION, catch_up=False)
    assert title == "Tonight: sell 2.8 kWh" and "force-discharge 18:00-18:20" in body and tags == ["battery"]
    title, body, tags = compose_evening({**DECISION, "opportunistic_export_kwh": 0.0, "credit_achievable": False},
                                        catch_up=True)
    assert title == "Tonight: self-use, nothing to sell" and "not winnable" in body and tags == ["warning"]
    assert "decided after a restart" in body


# ------------------------------------------------------------------- timing
class Recorder:
    def __init__(self):
        self.sent = []

    async def send(self, title, body, *, tags=(), priority=3):
        self.sent.append(title)
        return True


def _scheduler(tmp_path, monkeypatch, at):
    from zerohero_dynamic_control.data_providers.simulated import SimulatedSite
    from zerohero_dynamic_control.scheduler import ZeroHeroScheduler

    monkeypatch.setenv("NTFY_TOPIC", "zerohero-test")
    cfg = AppConfig()
    cfg.notify.enabled = True
    cfg.forecast.learn_load_days = 0
    cfg.logging.ledger_path = tmp_path / "ledger.jsonl"
    cfg.logging.decision_log_path = tmp_path / "decisions.jsonl"
    cfg.logging.samples_path = tmp_path / "samples.jsonl"
    site = SimulatedSite(cfg, solar_kw_at=lambda t: 0.0, load_kw_at=lambda t: 1.0)
    s = ZeroHeroScheduler(cfg, site)
    s.clock = SimClock(at)
    s.notifier = Recorder()
    return s


@pytest.mark.asyncio
async def test_the_morning_summary_waits_for_the_bill_then_goes_once(tmp_path, monkeypatch):
    s = _scheduler(tmp_path, monkeypatch, datetime(2026, 10, 3, 7, 30, tzinfo=TZ))
    await s.morning_summary_job(final=False)                 # 07:30 fetch, nothing published
    assert s.notifier.sent == []
    s.clock = SimClock(datetime(2026, 10, 3, 11, 0, tzinfo=TZ))
    await s.morning_summary_job(final=True)                  # 11:00 deadline: send without it
    assert len(s.notifier.sent) == 1 and "no evening record" in s.notifier.sent[0]
    s.ledger.record_bill(BILL)                               # 13:30 fetch brings the bill
    s.clock = SimClock(datetime(2026, 10, 3, 13, 30, tzinfo=TZ))
    await s.morning_summary_job(final=False)
    await s.morning_summary_job(final=False)                 # 16:30: nothing more
    assert s.notifier.sent[1] == "GloBird Fri 02 Oct: credit paid, day $0.49"
    assert len(s.notifier.sent) == 2


@pytest.mark.asyncio
async def test_with_the_bill_already_in_the_summary_goes_at_the_first_fetch(tmp_path, monkeypatch):
    s = _scheduler(tmp_path, monkeypatch, datetime(2026, 10, 3, 7, 30, tzinfo=TZ))
    s.ledger.record_bill(BILL)
    await s.morning_summary_job(final=False)
    s.clock = SimClock(datetime(2026, 10, 3, 11, 0, tzinfo=TZ))
    await s.morning_summary_job(final=True)
    assert s.notifier.sent == ["ZeroHero Fri 02 Oct: credit paid, day $0.49"]


@pytest.mark.asyncio
async def test_a_restart_does_not_resend(tmp_path, monkeypatch):
    s = _scheduler(tmp_path, monkeypatch, datetime(2026, 10, 3, 11, 0, tzinfo=TZ))
    await s.morning_summary_job(final=True)
    again = _scheduler(tmp_path, monkeypatch, datetime(2026, 10, 3, 11, 0, tzinfo=TZ))
    await again.morning_summary_job(final=True)
    assert len(s.notifier.sent) == 1 and again.notifier.sent == []


def test_every_line_is_a_bullet_or_an_indented_detail():
    """Readable on a phone: one item per bullet, details indented under it."""
    from zerohero_dynamic_control.notify import compose_bill_followup

    outcome = DailyOutcome(date="2026-10-02", exported_kwh=2.7, final_soc_pct=67.0, hourly_import=[])
    bodies = [
        compose_morning(date(2026, 10, 2), outcome=outcome, bill=BILL, decision=DECISION, free_window=None,
                        overnight=None, month_bills=[BILL], topup_rate=0.08, health=["Controller x"])[1],
        compose_morning(date(2026, 10, 2), outcome=None, bill=None, decision=None, free_window=None,
                        overnight=None, month_bills=[], topup_rate=0.08, health=[])[1],
        compose_evening({**DECISION, "credit_achievable": False, "telemetry_assumed": True}, catch_up=True)[1],
        compose_bill_followup(BILL, 0.08)[1],
    ]
    for body in bodies:
        for line in body.splitlines():
            assert line.startswith(("• ", "    ◦ ")), repr(line)
