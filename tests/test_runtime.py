"""Control loop behaviour: the setpoint law, guards and graceful degradation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from zerohero_dynamic_control.clock import SimClock
from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.controllers.base import SafetyWrapper
from zerohero_dynamic_control.controllers.printing import PrintingBatteryController
from zerohero_dynamic_control.data_providers.base import (
    CachingTelemetryProvider,
    ForecastProvider,
    ProviderError,
    TelemetryProvider,
)
from zerohero_dynamic_control.models import ForecastPoint, Telemetry, soc_to_energy
from zerohero_dynamic_control.runtime import EveningRunner

from .conftest import TZ


class BoomTelemetry(TelemetryProvider):
    name = "boom"

    def __init__(self, fail_after: int = 0):
        self.calls = 0
        self.fail_after = fail_after

    async def read(self, now: datetime) -> Telemetry:
        self.calls += 1
        if self.calls > self.fail_after:
            raise RuntimeError("inverter API timeout")
        return Telemetry(
            timestamp=now, soc_pct=80.0, battery_energy_kwh=soc_to_energy(80.0, 47.0),
            solar_kw=0.0, load_kw=2.0, grid_kw=2.0,
        )


class BoomForecast(ForecastProvider):
    name = "boom"

    async def solar(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        raise RuntimeError("solcast is down")

    async def load(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        raise RuntimeError("no load history")


def build_runner(cfg: AppConfig, telemetry: TelemetryProvider, forecast: ForecastProvider, now: datetime):
    controller = SafetyWrapper(
        PrintingBatteryController(quiet=True),
        max_power_kw=cfg.inverter.ac_limit_kw,
        min_soc_pct=cfg.battery.emergency_floor_soc_pct,
    )
    return EveningRunner(
        cfg, telemetry=telemetry, forecast=forecast, controller=controller,
        ledger=None, clock=SimClock(now),
    )


@pytest.mark.asyncio
async def test_plans_even_when_every_forecast_fails():
    """Graceful degradation: a dead forecast must not stop the decision. Assuming
    zero solar is the safe direction — it over-reserves battery rather than
    under-reserving and importing."""
    cfg = AppConfig()
    now = datetime(2026, 1, 15, 17, 50, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(fail_after=99), BoomForecast(), now)
    decision = await runner.make_decision()
    assert decision.degraded
    assert decision.mandatory_discharge_kwh > 0
    assert any("DEGRADED" in r for r in decision.rationale)


@pytest.mark.asyncio
async def test_stale_telemetry_falls_back_to_fixed_power():
    """Requirement 4: 'fall back to simple force export at X kW until 21:00'."""
    cfg = AppConfig()
    now = datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), now)
    stale = Telemetry(
        timestamp=now, soc_pct=70.0, battery_energy_kwh=soc_to_energy(70.0, 47.0),
        solar_kw=0.0, load_kw=2.0, grid_kw=2.0, stale=True,
    )
    setpoint, reason = runner.compute_setpoint(stale, now)
    assert setpoint == pytest.approx(cfg.strategy.fallback_discharge_kw)
    assert "FALLBACK" in reason


@pytest.mark.asyncio
async def test_caching_provider_serves_last_good_reading():
    inner = BoomTelemetry(fail_after=1)
    cache = CachingTelemetryProvider(inner, max_age_seconds=600)
    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    first = await cache.read(now)
    assert not first.stale
    second = await cache.read(now + timedelta(minutes=1))
    assert second.stale and second.soc_pct == first.soc_pct


@pytest.mark.asyncio
async def test_caching_provider_gives_up_once_too_stale():
    cache = CachingTelemetryProvider(BoomTelemetry(fail_after=1), max_age_seconds=60)
    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await cache.read(now)
    with pytest.raises(ProviderError):
        await cache.read(now + timedelta(minutes=30))


def test_setpoint_covers_net_load_plus_margin():
    """The control law: battery >= load - solar, plus a deliberate export margin."""
    now = datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    cfg = AppConfig()
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), now)
    tel = Telemetry(
        timestamp=now, soc_pct=80.0, battery_energy_kwh=soc_to_energy(80.0, 47.0),
        solar_kw=1.5, load_kw=4.0, battery_kw=0.0, grid_kw=2.5,
    )
    setpoint, _ = runner.compute_setpoint(tel, now)
    assert setpoint >= 2.5 + cfg.strategy.import_safety_margin_kw - 1e-6


def test_setpoint_respects_the_shared_ac_port():
    """8 kW of PV leaves only 2 kW of battery through a 10 kW hybrid — equation (4)."""
    cfg = AppConfig()
    now = datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), now)
    tel = Telemetry(
        timestamp=now, soc_pct=95.0, battery_energy_kwh=soc_to_energy(95.0, 47.0),
        solar_kw=8.0, load_kw=9.5, battery_kw=0.0, grid_kw=1.5,
    )
    setpoint, _ = runner.compute_setpoint(tel, now)
    assert setpoint <= cfg.inverter.ac_limit_kw - 8.0 + 1e-6


def test_manual_override_wins():
    cfg = AppConfig()
    now = datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), now)
    runner.manual_override_kw = 6.0
    tel = Telemetry(
        timestamp=now, soc_pct=80.0, battery_energy_kwh=soc_to_energy(80.0, 47.0),
        solar_kw=0.0, load_kw=1.0, grid_kw=1.0,
    )
    assert runner.compute_setpoint(tel, now)[0] == 6.0


def test_safety_wrapper_blocks_discharge_at_the_floor():
    cfg = AppConfig()
    wrapper = SafetyWrapper(
        PrintingBatteryController(quiet=True),
        max_power_kw=cfg.inverter.ac_limit_kw,
        min_soc_pct=cfg.battery.emergency_floor_soc_pct,
    )
    wrapper.observe_soc(9.0)
    assert wrapper._clamp(5.0) == 0.0
    assert any("blocked" in v for v in wrapper.violations)


def test_safety_wrapper_clamps_to_the_inverter_limit():
    cfg = AppConfig()
    wrapper = SafetyWrapper(
        PrintingBatteryController(quiet=True),
        max_power_kw=cfg.inverter.ac_limit_kw,
        min_soc_pct=cfg.battery.emergency_floor_soc_pct,
    )
    wrapper.observe_soc(90.0)
    assert wrapper._clamp(25.0) == cfg.inverter.ac_limit_kw
    assert any("clamped" in v for v in wrapper.violations)


def test_margin_escalates_as_the_hourly_allowance_is_consumed():
    from zerohero_dynamic_control.credit_monitor import CreditMonitor

    cfg = AppConfig()
    now = datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), now)
    start = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    runner.monitor = CreditMonitor(start, start + timedelta(hours=3))
    clean = runner._margin_kw(now)
    # Burn most of the 18:00 hour's allowance.
    runner.monitor.observe(start, 1.5)
    runner.monitor.observe(start + timedelta(minutes=1), 1.5)
    assert runner._margin_kw(now) > clean
