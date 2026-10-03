"""Overnight drain: learned from history, cut short by tomorrow's sun."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.ledger import Ledger
from zerohero_dynamic_control.models import ForecastPoint, ObjectiveMode
from zerohero_dynamic_control.overnight import (
    OvernightRecord,
    learned_drain,
    overnight_need,
    record_from_history,
)

from .conftest import TZ, at, flat, make_telemetry

DAY = date(2026, 9, 29)


def _history(socs, *, imports=0.0, start=datetime(2026, 9, 29, 20, 30), step=5):
    """A FoxESS history payload: SoC every `step` minutes from `start`."""
    pts = [start + timedelta(minutes=step * i) for i in range(len(socs))]
    fmt = "%Y-%m-%d %H:%M:%S"
    return [{"datas": [
        {"variable": "SoC", "data": [{"time": t.strftime(fmt) + " AEST+1000", "value": v} for t, v in zip(pts, socs, strict=True)]},
        {"variable": "gridConsumptionPower", "data": [{"time": t.strftime(fmt), "value": imports} for t in pts]},
    ]}]


def _night(soc21=67.0, rate_pct_h=4.0, low_at_min=600, sunny_rise=6.0):
    """21:00 -> 14:30: drain at rate_pct_h until low_at_min after 21:00, then rise."""
    socs, soc = [], soc21
    for i in range(0, 18 * 12):               # 20:30 + 5 min steps to 14:30
        minute = i * 5 - 30
        if 0 < minute <= low_at_min:
            soc -= rate_pct_h / 12
        elif minute > low_at_min:
            soc = min(100.0, soc + sunny_rise / 12)
        socs.append(round(soc, 3))
    return socs


def test_a_night_record_from_history():
    rec = record_from_history(_history(_night(), imports=0.05), DAY, capacity_kwh=47.0, tz=TZ)
    assert rec is not None and rec.date == "2026-09-29"
    assert rec.soc_21 == pytest.approx(67, abs=1) and rec.soc_04 == pytest.approx(39, abs=1)
    assert rec.drain_kwh_per_h == pytest.approx(4.0 * 0.47, abs=0.05)
    assert rec.low_at == "07:00" and rec.import_kwh == pytest.approx(0.05 * 14, abs=0.01)


def test_history_that_misses_the_night_is_not_recorded():
    short = _history(_night()[:100])
    assert record_from_history(short, DAY, capacity_kwh=47.0, tz=TZ) is None


def _rec(day, rate):
    return OvernightRecord(date=day.isoformat(), soc_21=67, soc_04=39, drain_kwh_per_h=rate,
                           low_soc=29, low_at="07:00", soc_11=58, import_kwh=0.5)


def test_the_drain_is_the_median_of_recent_nights_before_tonight():
    recs = [_rec(DAY - timedelta(days=k), r) for k, r in ((1, 1.7), (2, 1.9), (3, 1.8), (4, 3.5), (30, 9.0))]
    rate, n = learned_drain(recs, before=DAY, days=7)
    assert (rate, n) == (pytest.approx(1.85), 4), "median of the last week; the month-old night is out"
    assert learned_drain(recs[:2], before=DAY) is None, "two nights are not enough"
    assert learned_drain([_rec(DAY, 1.0)] * 5, before=DAY) is None, "tonight cannot teach tonight"


def _sun(rise_hour):
    """0 kW before rise_hour, then 1 kW more each hour (cap 5)."""
    def pv(t):
        h = t.hour + t.minute / 60
        return 0.0 if h < rise_hour or h > 17 else min(5.0, h - rise_hour)
    return pv


def test_a_sunny_morning_ends_the_night_early_and_a_cloudy_one_does_not():
    start = datetime(2026, 9, 29, 21, tzinfo=TZ)
    end = start + timedelta(hours=14)
    sunny_need, sunny_low = overnight_need(1.88, _sun(6.0), start, end)
    dull_need, dull_low = overnight_need(1.88, lambda t: 0.0, start, end)
    assert sunny_low.strftime("%H:%M") == "08:00" and dull_low == end
    assert dull_need == pytest.approx(1.88 * 14)
    assert sunny_need < dull_need - 5, "the forecast is worth ~5 kWh of selling on a sunny morning"


def test_the_engine_holds_the_measured_need_instead_of_the_fixed_estimate(cfg, engine):
    cfg.strategy.objective = ObjectiveMode.RETAIN_OVERNIGHT
    now = at(2026, 9, 29)
    tel = make_telemetry(cfg, now, 97.0, 2.0)
    fixed = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(2.0))
    weather = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(2.0),
                          overnight_need_kwh=15.0, overnight_note="holding 15.0 kWh for the night: test")
    assert weather.opportunistic_export_kwh > fixed.opportunistic_export_kwh
    assert any("holding 15.0 kWh for the night: test" in r for r in weather.rationale)


class _Forecast:
    def __init__(self, rise=None, fail=False):
        self.rise, self.fail = rise, fail

    async def solar(self, start, end):
        if self.fail:
            raise RuntimeError("open-meteo down")
        pv = _sun(self.rise) if self.rise is not None else (lambda t: 0.0)
        pts, t = [], start
        while t <= end:
            pts.append(ForecastPoint(timestamp=t, solar_kw=pv(t)))
            t += timedelta(minutes=15)
        return pts

    async def load(self, start, end):
        return []


@pytest.mark.asyncio
async def test_the_runner_learns_from_the_ledger_and_falls_back_safely(tmp_path):
    from .test_runtime import BoomTelemetry, build_runner

    cfg = AppConfig()
    cfg.strategy.objective = ObjectiveMode.RETAIN_OVERNIGHT
    window_end = datetime(2026, 9, 29, 21, tzinfo=TZ)
    ledger = Ledger(tmp_path / "l.jsonl", tmp_path / "d.jsonl")
    runner = build_runner(cfg, BoomTelemetry(), _Forecast(rise=6.0), window_end)
    runner.ledger = ledger

    need, note = await runner._overnight_need(window_end)
    assert "assumed: not enough history yet" in note

    for k in (1, 2, 3):
        ledger.record_overnight(_rec(DAY - timedelta(days=k), 1.88))
    need, note = await runner._overnight_need(window_end)
    assert "learned from 3 nights" in note and "about 08:00" in note
    assert need == pytest.approx(overnight_need(1.88, _sun(6.0), window_end, window_end + timedelta(hours=14))[0])

    runner.forecast = _Forecast(fail=True)
    dull, note = await runner._overnight_need(window_end)
    assert dull == pytest.approx(1.88 * 14) and "until the free window" in note, \
        "no forecast: assume no morning sun"

    cfg.strategy.weather_aware_overnight = False
    assert await runner._overnight_need(window_end) is None
