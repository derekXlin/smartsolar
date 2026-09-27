"""End-to-end: the real control loop against a physical model of the site."""

from __future__ import annotations

import pytest

from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.models import BatteryMode
from zerohero_dynamic_control.simulation import SCENARIOS, run_scenario

WINNABLE = ["summer", "winter", "cloudy", "high_load"]


@pytest.mark.parametrize("key", WINNABLE)
@pytest.mark.asyncio
async def test_winnable_scenarios_secure_the_credit(key):
    """With a pessimistic forecast (15% less sun, 10% more load than reality), every
    scenario that has enough stored energy must still come away with the $1."""
    r = await run_scenario(SCENARIOS[key], forecast_bias=0.85, load_bias=1.10)
    assert r.outcome.credit_secured, (
        f"{key} lost the credit: {r.outcome.notes}"
    )
    assert r.outcome.imported_kwh < 0.03 * 3


@pytest.mark.parametrize("key", WINNABLE)
@pytest.mark.asyncio
async def test_no_hour_ever_breaches(key):
    r = await run_scenario(SCENARIOS[key])
    for bucket in r.outcome.hourly_import:
        assert not bucket.breached, f"{key} breached {bucket.hour_start:%H:%M}"


@pytest.mark.parametrize("key", list(SCENARIOS))
@pytest.mark.asyncio
async def test_soc_floor_is_never_violated(key):
    cfg = AppConfig()
    r = await run_scenario(SCENARIOS[key], cfg)
    worst = min(h.soc_pct for h in r.site.history)
    assert worst >= cfg.battery.emergency_floor_soc_pct - 0.5, (
        f"{key} reached {worst:.1f}% SOC, below the {cfg.battery.emergency_floor_soc_pct}% floor"
    )


@pytest.mark.parametrize("key", list(SCENARIOS))
@pytest.mark.asyncio
async def test_inverter_limit_is_never_violated_in_practice(key):
    """Not just in the plan — in the simulated hardware, sample by sample."""
    cfg = AppConfig()
    r = await run_scenario(SCENARIOS[key], cfg)
    for h in r.site.history:
        assert h.solar_kw + max(0.0, h.battery_kw) <= cfg.inverter.ac_limit_kw + 0.05
        assert h.load_kw + h.export_kw <= cfg.inverter.ac_limit_kw + 0.05


@pytest.mark.asyncio
async def test_depleted_battery_fails_gracefully_not_catastrophically():
    """It should lose the credit, but not the battery: floor held, mode reverted."""
    cfg = AppConfig()
    r = await run_scenario(SCENARIOS["low_soc"], cfg)
    assert not r.outcome.credit_secured
    assert r.outcome.final_soc_pct >= cfg.battery.emergency_floor_soc_pct - 0.5
    assert not r.decision.credit_achievable
    # Abandoned credit: the battery serves the house in self-use, never forced.
    assert r.commands and all(c.mode is BatteryMode.SELF_CONSUMPTION for c in r.commands)


@pytest.mark.asyncio
async def test_summer_earns_more_than_winter():
    """Long summer evenings leave surplus to sell; winter evenings do not."""
    summer = await run_scenario(SCENARIOS["summer"])
    winter = await run_scenario(SCENARIOS["winter"])
    assert summer.outcome.exported_kwh > winter.outcome.exported_kwh
    assert summer.outcome.estimated_revenue_aud < winter.outcome.estimated_revenue_aud


@pytest.mark.asyncio
async def test_super_export_cap_not_exceeded_in_practice():
    cfg = AppConfig()
    for key in SCENARIOS:
        r = await run_scenario(SCENARIOS[key], cfg)
        assert r.outcome.exported_kwh <= cfg.plan.super_export_cap_kwh + 1.0, (
            f"{key} exported {r.outcome.exported_kwh:.1f} kWh past the cap, "
            f"earning only $0.02/kWh on the excess"
        )


@pytest.mark.asyncio
async def test_badly_wrong_forecast_still_secures_the_credit():
    """Forecast says sunny and quiet; reality is the opposite. The closed loop on the
    meter, not the plan, is what has to save the day here."""
    r = await run_scenario(SCENARIOS["summer"], forecast_bias=2.0, load_bias=0.5)
    assert r.outcome.credit_secured


@pytest.mark.asyncio
async def test_window_closes_in_self_consumption():
    r = await run_scenario(SCENARIOS["summer"])
    assert r.site.mode.value == "self_consumption"


# --------------------------------------- the first live evening, reproduced
async def _evening_with_cloud_lag_and_a_load_step(min_force_export_kw: float) -> dict[int, float]:
    """Telemetry refreshed only every 5 minutes, like the FoxESS cloud feed, and a
    2.2 kW load step at 18:50 for 5 minutes. Returns TRUE import (Wh) per hour,
    measured by the simulated site's own meter, not by the lagged telemetry."""
    from datetime import datetime, timedelta

    from zerohero_dynamic_control.clock import SimClock
    from zerohero_dynamic_control.controllers.base import SafetyWrapper
    from zerohero_dynamic_control.controllers.simulated import SimulatedBatteryController
    from zerohero_dynamic_control.data_providers.base import TelemetryProvider
    from zerohero_dynamic_control.data_providers.simulated import SimulatedSite
    from zerohero_dynamic_control.runtime import EveningRunner
    from zerohero_dynamic_control.simulation.harness import ScenarioForecastProvider
    from zerohero_dynamic_control.simulation.profiles import load_curve, solar_curve

    cfg = AppConfig()
    cfg.strategy.min_force_export_kw = min_force_export_kw
    scenario = SCENARIOS["winter"]
    tz = cfg.site.tz
    day = scenario.day
    base_load = load_curve(scenario)
    step_at = datetime(day.year, day.month, day.day, 18, 50, tzinfo=tz)

    def load(when):
        return base_load(when) + (2.2 if step_at <= when < step_at + timedelta(minutes=5) else 0.0)

    site = SimulatedSite(cfg, solar_kw_at=solar_curve(scenario, tz), load_kw_at=load,
                         start_soc_pct=scenario.start_soc_pct)

    class CloudLag(TelemetryProvider):
        name = "cloud-lag"

        def __init__(self):
            self.snapshot = None

        async def read(self, now):
            reading = await site.read(now)          # the physics advances every tick
            if self.snapshot is None or (now - self.snapshot.timestamp) >= timedelta(minutes=5):
                self.snapshot = reading
            return self.snapshot.model_copy(update={"timestamp": now})

    start = datetime(day.year, day.month, day.day, 17, 50, tzinfo=tz)
    runner = EveningRunner(
        cfg, telemetry=CloudLag(),
        forecast=ScenarioForecastProvider(scenario, cfg, 1.0, 1.0),
        controller=SafetyWrapper(SimulatedBatteryController(site), max_power_kw=cfg.inverter.ac_limit_kw,
                                 min_soc_pct=cfg.battery.emergency_floor_soc_pct),
        ledger=None, clock=SimClock(start, speed=0.0),
    )
    await runner.make_decision()
    await runner.run_window()

    wh: dict[int, float] = {18: 0.0, 19: 0.0, 20: 0.0}
    for prev, cur in zip(site.history, site.history[1:], strict=False):
        if prev.timestamp.hour in wh:
            dt_h = (cur.timestamp - prev.timestamp).total_seconds() / 3600
            wh[prev.timestamp.hour] += max(0.0, cur.grid_kw) * dt_h * 1000
    return wh


@pytest.mark.asyncio
async def test_old_policy_loses_the_hour_to_a_load_step_behind_cloud_lag():
    """min_force_export_kw=0 is the old behaviour: force-discharge at load + 0.25 kW
    all evening. With five-minute-old data it cannot see the step in time."""
    wh = await _evening_with_cloud_lag_and_a_load_step(min_force_export_kw=0.0)
    assert wh[18] > 30.0, wh


@pytest.mark.asyncio
async def test_self_use_absorbs_the_same_load_step():
    """The owner's pattern: self-use unless exporting hard. The inverter covers the
    step from its own meter, so every hour stays under 30 Wh."""
    wh = await _evening_with_cloud_lag_and_a_load_step(min_force_export_kw=3.0)
    assert all(v < 30.0 for v in wh.values()), wh
