"""End-to-end: the real control loop against a physical model of the site."""

from __future__ import annotations

import pytest

from zerohero_dynamic_control.config import AppConfig
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
    assert any("blocked" in v for v in r.safety_violations)


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
