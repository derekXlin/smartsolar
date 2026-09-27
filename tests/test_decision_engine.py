"""Decision engine behaviour across realistic evenings."""

from __future__ import annotations

import pytest

from zerohero_dynamic_control.models import BatteryMode, ObjectiveMode, RiskLevel, soc_to_energy

from .conftest import at, flat, make_telemetry, solar_fn


# --------------------------------------------------------------------- summer
def test_summer_high_residual_solar_plans_export(cfg, engine):
    """Mid-January: sunset ~20:09, so PV covers much of the window and the pack,
    already near full, has surplus that tomorrow's free window would strand."""
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=92.0, load_kw=1.6, solar_kw=3.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=solar_fn(10.0), load_kw_at=flat(1.6))

    assert d.credit_achievable
    assert d.recommended_mode is BatteryMode.FORCE_EXPORT
    assert d.opportunistic_export_kwh > 0, "a near-full pack in summer should sell surplus"
    assert d.expected_final_soc > cfg.battery.min_reserve_soc_pct
    # Residual PV means the battery supplies less than the raw house load.
    assert d.mandatory_discharge_kwh < 1.6 * 3


def test_winter_zero_residual_solar_battery_carries_window(cfg, engine):
    """Winter solstice: sunset 16:55, so there is no PV at all after 18:00 and the
    battery must supply the entire house load for three hours."""
    now = at(2026, 6, 21)
    tel = make_telemetry(cfg, now, soc_pct=85.0, load_kw=3.5)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=solar_fn(10.0), load_kw_at=flat(3.5))

    assert d.credit_achievable
    assert all(s.solar_kw == pytest.approx(0.0, abs=1e-6) for s in d.slots)
    expected = (3.5 + cfg.strategy.import_safety_margin_kw) * 3
    assert d.mandatory_discharge_kwh == pytest.approx(expected, rel=0.02)
    # Far less surplus to sell than in summer.
    assert d.opportunistic_export_kwh < 4.0


def test_winter_exports_less_than_summer(cfg, engine):
    """The seasonal difference must actually show up in the plan."""
    summer = engine.plan(
        now=at(2026, 1, 15),
        telemetry=make_telemetry(cfg, at(2026, 1, 15), 90.0, 2.0),
        solar_kw_at=solar_fn(10.0), load_kw_at=flat(2.0),
    )
    winter = engine.plan(
        now=at(2026, 6, 21),
        telemetry=make_telemetry(cfg, at(2026, 6, 21), 90.0, 2.0),
        solar_kw_at=solar_fn(10.0), load_kw_at=flat(2.0),
    )
    assert summer.target_export_kwh > winter.target_export_kwh


# ------------------------------------------------------------------ hard limits
@pytest.mark.parametrize("load_kw", [0.5, 2.0, 5.0, 8.5, 9.8])
def test_inverter_ac_limit_never_exceeded(cfg, engine, load_kw):
    """Equation (5): load + export must stay inside the 10 kW hybrid AC limit, and
    equation (4): solar + battery must too. This is the constraint a naive planner
    misses, because solar competes for the port rather than adding headroom."""
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=95.0, load_kw=load_kw)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=solar_fn(10.0), load_kw_at=flat(load_kw))

    limit = cfg.inverter.ac_limit_kw
    for s in d.slots:
        assert s.solar_kw + s.battery_ac_kw <= limit + 1e-6, f"AC port overloaded at {s.start:%H:%M}"
        assert s.load_kw + s.grid_export_kw <= limit + 1e-6, f"load+export over limit at {s.start:%H:%M}"
        assert s.battery_ac_kw <= cfg.battery.max_discharge_kw + 1e-6


def test_high_evening_load_collapses_export_headroom(cfg, engine):
    """An 8.5 kW oven+EV evening leaves only ~1.5 kW of export room under the limit."""
    now = at(2026, 3, 10)
    tel = make_telemetry(cfg, now, soc_pct=90.0, load_kw=8.5)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(8.5))

    for s in d.slots:
        assert s.export_headroom_kw <= cfg.inverter.ac_limit_kw - 8.5 + 1e-6
    assert d.peak_discharge_kw <= cfg.inverter.ac_limit_kw + 1e-6


@pytest.mark.parametrize("soc", [12.0, 20.0, 30.0, 50.0, 75.0, 100.0])
def test_never_plans_below_emergency_floor(cfg, engine, soc):
    """Requirement 4: never discharge below the user-defined minimum reserve."""
    now = at(2026, 6, 21)
    tel = make_telemetry(cfg, now, soc_pct=soc, load_kw=4.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(4.0))
    assert d.expected_final_soc >= cfg.battery.emergency_floor_soc_pct - 1e-6, (
        f"plan from {soc}% SOC ends at {d.expected_final_soc}%, below the hard floor"
    )


def test_grid_export_limit_respected(cfg, engine):
    cfg.inverter.grid_export_limit_kw = 5.0
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=100.0, load_kw=1.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(1.0))
    for s in d.slots:
        assert s.grid_export_kw <= 5.0 + 1e-6


# --------------------------------------------------------------- edge cases
def test_depleted_battery_abandons_credit_cleanly(cfg, engine):
    """If the window cannot be covered, the credit is lost regardless — so stop
    spending charge on it and keep the energy for cheaper self-consumption."""
    now = at(2026, 6, 21)
    tel = make_telemetry(cfg, now, soc_pct=20.0, load_kw=4.5)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(4.5))

    assert not d.credit_achievable
    assert d.risk is RiskLevel.AT_RISK
    assert d.recommended_mode is BatteryMode.SELF_CONSUMPTION
    assert d.energy_shortfall_kwh > 0
    assert d.opportunistic_export_kwh == pytest.approx(0.0, abs=1e-6)
    assert any("ABANDONING" in r for r in d.rationale)


def test_marginal_shortfall_still_attempted(cfg, engine):
    """A shortfall inside the forecast-noise margin should not trigger abandonment."""
    cfg.strategy.unwinnable_margin_kwh = 5.0
    now = at(2026, 6, 21)
    tel = make_telemetry(cfg, now, soc_pct=35.0, load_kw=4.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(4.0))
    assert d.recommended_mode is BatteryMode.FORCE_EXPORT


def test_cloudy_low_soc_prioritises_credit_over_export(cfg, engine):
    now = at(2026, 9, 27)
    tel = make_telemetry(cfg, now, soc_pct=45.0, load_kw=2.8)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=solar_fn(10.0, cloud=0.1), load_kw_at=flat(2.8))
    assert d.credit_achievable
    assert d.opportunistic_export_kwh == pytest.approx(0.0, abs=0.01)


def test_planning_mid_window_only_covers_the_remainder(cfg, engine):
    """A restart at 19:30 must plan 90 minutes, not three hours."""
    now = at(2026, 6, 21, hour=19, minute=30)
    tel = make_telemetry(cfg, now, soc_pct=70.0, load_kw=3.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(3.0))
    assert d.slots[0].start == now
    assert sum(s.hours for s in d.slots) == pytest.approx(1.5, abs=0.01)


def test_planning_after_window_rolls_to_tomorrow(cfg, engine):
    now = at(2026, 6, 21, hour=22, minute=0)
    tel = make_telemetry(cfg, now, soc_pct=70.0, load_kw=3.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(3.0))
    assert d.window_start.date() == now.date().replace(day=22)


# ------------------------------------------------------------------ objectives
def test_super_export_cap_is_respected(cfg, engine):
    """Beyond ~15 kWh the top-up stops and export drops to $0.02, below the $0.407
    the energy is worth in the battery — so the planner must stop at the cap."""
    cfg.strategy.objective = ObjectiveMode.MAXIMISE_EXPORT
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=100.0, load_kw=1.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(1.0))
    assert d.target_export_kwh <= cfg.plan.super_export_cap_kwh + 1e-6


def test_guarantee_credit_only_sells_nothing(cfg, engine):
    cfg.strategy.objective = ObjectiveMode.GUARANTEE_CREDIT_ONLY
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=100.0, load_kw=1.5)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(1.5))
    assert d.opportunistic_export_kwh == pytest.approx(0.0, abs=1e-6)


def test_objectives_ordered_by_aggression(cfg, engine):
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=95.0, load_kw=1.5)
    results = {}
    for obj in (ObjectiveMode.GUARANTEE_CREDIT_ONLY, ObjectiveMode.ECONOMIC,
                ObjectiveMode.RETAIN_OVERNIGHT, ObjectiveMode.MAXIMISE_EXPORT):
        cfg.strategy.objective = obj
        results[obj] = engine.plan(
            now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(1.5)
        ).opportunistic_export_kwh
    assert results[ObjectiveMode.GUARANTEE_CREDIT_ONLY] <= results[ObjectiveMode.ECONOMIC]
    assert results[ObjectiveMode.ECONOMIC] <= results[ObjectiveMode.MAXIMISE_EXPORT]


def test_sunny_morning_forecast_increases_export(cfg, engine):
    """A big solar morning refills the pack for free, which makes tonight's surplus
    stranded — so the engine should sell more of it."""
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=90.0, load_kw=1.5)
    kwargs = dict(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(1.5))
    dull = engine.plan(**kwargs, morning_solar_to_battery_kwh=1.0)
    bright = engine.plan(**kwargs, morning_solar_to_battery_kwh=18.0)
    assert bright.opportunistic_export_kwh > dull.opportunistic_export_kwh


# ------------------------------------------------------------------ allocation
@pytest.mark.parametrize("shape", ["constant", "front_loaded", "solar_following"])
def test_allocation_conserves_the_budget(cfg, engine, shape):
    from zerohero_dynamic_control.models import AllocationShape

    cfg.strategy.allocation = AllocationShape(shape)
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=95.0, load_kw=1.5)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=solar_fn(10.0), load_kw_at=flat(1.5))
    for s in d.slots:
        assert s.export_discharge_kw <= s.export_headroom_kw + 1e-6


def test_front_loaded_sells_earlier_than_constant(cfg, engine):
    from zerohero_dynamic_control.models import AllocationShape

    now = at(2026, 6, 21)
    tel = make_telemetry(cfg, now, soc_pct=95.0, load_kw=1.0)
    kwargs = dict(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(1.0))

    cfg.strategy.allocation = AllocationShape.FRONT_LOADED
    front = engine.plan(**kwargs)
    cfg.strategy.allocation = AllocationShape.CONSTANT
    const = engine.plan(**kwargs)

    def first_hour(d):
        return sum(s.export_discharge_kw * s.hours for s in d.slots[: len(d.slots) // 3])

    assert first_hour(front) > first_hour(const)


# --------------------------------------------------------------------- outputs
def test_decision_reports_the_three_required_outputs(cfg, engine):
    now = at(2026, 1, 15)
    tel = make_telemetry(cfg, now, soc_pct=88.0, load_kw=2.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=solar_fn(10.0), load_kw_at=flat(2.0))
    assert d.target_export_kwh >= 0
    assert d.recommended_discharge_kw >= 0
    assert 0 <= d.expected_final_soc <= 100
    assert "target_export_kwh" in d.summary()


def test_energy_balance_is_self_consistent(cfg, engine):
    """Planned DC draw must equal the SOC drop implied by the plan."""
    now = at(2026, 6, 21)
    tel = make_telemetry(cfg, now, soc_pct=80.0, load_kw=3.0)
    d = engine.plan(now=now, telemetry=tel, solar_kw_at=flat(0.0), load_kw_at=flat(3.0))
    implied = soc_to_energy(d.starting_soc_pct - d.expected_final_soc, cfg.battery.usable_capacity_kwh)
    assert implied == pytest.approx(d.battery_dc_drawn_kwh, rel=0.01)


def test_block_allocation_exports_at_full_power_first(cfg, engine):
    """The owner's pattern: sell hard at the start, then stop. A thin export spread
    over three hours is too small to act as a buffer, so it would buy no safety."""
    from zerohero_dynamic_control.models import AllocationShape

    cfg.strategy.allocation = AllocationShape.BLOCK
    cfg.strategy.objective = ObjectiveMode.MAXIMISE_EXPORT
    now = at(2026, 1, 15)
    d = engine.plan(now=now, telemetry=make_telemetry(cfg, now, 95.0, 1.5),
                    solar_kw_at=flat(0.0), load_kw_at=flat(1.5))
    exports = [s.export_discharge_kw for s in d.slots]
    first_zero = next((i for i, e in enumerate(exports) if e < 1e-9), len(exports))
    assert first_zero > 0 and all(e < 1e-9 for e in exports[first_zero:]), "one contiguous block"
    full = [e for e, s in zip(exports[:first_zero], d.slots, strict=False)
            if abs(e - s.export_headroom_kw) < 1e-6]
    assert len(full) >= first_zero - 1, "every slot but the last runs at full headroom"
    assert any(r.startswith("control: force-discharge 18:00") for r in d.rationale)
