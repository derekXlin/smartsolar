"""Assurance for the 11:00-14:00 free charging window."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from zerohero_dynamic_control.clock import SimClock
from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.controllers.printing import PrintingBatteryController
from zerohero_dynamic_control.data_providers.base import TelemetryProvider
from zerohero_dynamic_control.free_window import FreeChargeAssurance, audit_schedule
from zerohero_dynamic_control.models import Telemetry, soc_to_energy

from .conftest import TZ

FREE_START, FREE_END = time(11, 0), time(14, 0)

GOOD = {"enable": 1, "startHour": 11, "startMinute": 0, "endHour": 13, "endMinute": 59,
        "workMode": "ForceCharge"}


def audit(groups):
    return audit_schedule(groups, window_start=FREE_START, window_end=FREE_END)


# ------------------------------------------------------- configuration audit
def test_correct_force_charge_group_passes():
    ok, findings = audit([GOOD])
    assert ok, findings


def test_no_force_charge_group_at_all_fails():
    """The expensive silent failure: the group was deleted and nobody noticed."""
    ok, findings = audit([{"enable": 1, "startHour": 11, "endHour": 13, "workMode": "SelfUse"}])
    assert not ok
    assert "NO ForceCharge group" in findings[0]


def test_empty_schedule_fails():
    ok, findings = audit([])
    assert not ok


def test_disabled_group_is_caught():
    """Present, correct times, but enable=0 — looks fine in a casual glance."""
    ok, findings = audit([{**GOOD, "enable": 0}])
    assert not ok
    assert "enable flag is 0" in findings[0]


def test_group_moved_outside_the_window_is_caught():
    """Someone edited the times in the app to 02:00-05:00."""
    ok, findings = audit([{**GOOD, "startHour": 2, "endHour": 4, "endMinute": 59}])
    assert not ok
    assert "does not overlap" in findings[0]


def test_partial_coverage_is_caught():
    """Covers 11:00-12:00 only: two thirds of the free energy is unreachable."""
    ok, findings = audit([{**GOOD, "endHour": 11, "endMinute": 59}])
    assert not ok
    assert "minutes of $0.00 energy are unreachable" in findings[0]


def test_two_groups_covering_the_window_between_them_pass():
    ok, _ = audit([
        {**GOOD, "startHour": 11, "endHour": 12, "endMinute": 29},
        {**GOOD, "startHour": 12, "startMinute": 30, "endHour": 13, "endMinute": 59},
    ])
    assert ok


def test_force_discharge_group_does_not_count_as_charging():
    ok, _ = audit([{**GOOD, "workMode": "ForceDischarge"}])
    assert not ok


# -------------------------------------------------------- behaviour checking
class ScriptedSite(TelemetryProvider):
    """Replays a fixed sequence of (soc, battery_kw, solar_kw, import_kw)."""

    name = "scripted"

    def __init__(self, script, capacity=47.0):
        self.script = list(script)
        self.capacity = capacity
        self.i = 0

    async def read(self, now):
        soc, batt, solar, imp = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return Telemetry(
            timestamp=now, soc_pct=soc,
            battery_energy_kwh=soc_to_energy(soc, self.capacity),
            solar_kw=solar, load_kw=1.5, battery_kw=batt, grid_kw=imp,
        )


def build(cfg, site, groups=None):
    ctl = PrintingBatteryController(quiet=True)
    if groups is not None:
        async def _reader():
            return groups
        ctl.attach_schedule_reader(_reader)
    clock = SimClock(datetime(2026, 9, 27, 10, 50, tzinfo=TZ))
    return FreeChargeAssurance(cfg, telemetry=site, controller=ctl, clock=clock, ledger=None)


def fast_cfg() -> AppConfig:
    cfg = AppConfig()
    cfg.strategy.free_window_assurance.check_interval_seconds = 1800  # 6 samples
    return cfg


@pytest.mark.asyncio
async def test_healthy_window_passes_end_to_end():
    cfg = fast_cfg()
    # Charging hard from grid, climbing 40% -> 100%.
    script = [(40 + i * 12, -9.5, 0.5, 9.0) for i in range(6)] + [(100.0, 0.0, 0.5, 0.0)]
    outcome = await build(cfg, ScriptedSite(script), [GOOD]).run_window()

    assert outcome.ok
    assert outcome.schedule_ok
    assert outcome.failed_samples == 0
    assert outcome.final_soc_pct >= cfg.strategy.free_window_assurance.alert_soc_pct
    assert outcome.energy_added_kwh > 0


@pytest.mark.asyncio
async def test_idle_battery_with_headroom_is_flagged():
    """The failure that costs real money: the window opens and nothing happens."""
    cfg = fast_cfg()
    outcome = await build(cfg, ScriptedSite([(45.0, 0.0, 0.5, 0.0)] * 8), [GOOD]).run_window()

    assert not outcome.ok
    assert outcome.failed_samples > 0
    assert outcome.unclaimed_free_kwh > 20
    # ~25 kWh not taken for free, to be bought later at ~$0.42
    assert outcome.missed_value_aud > 8
    assert any("NOT charging" in f for f in outcome.findings)


@pytest.mark.asyncio
async def test_already_full_battery_is_not_a_failure():
    """Nothing to charge is a pass, not a fault."""
    cfg = fast_cfg()
    outcome = await build(cfg, ScriptedSite([(100.0, 0.0, 2.0, 0.0)] * 8), [GOOD]).run_window()
    assert outcome.ok
    assert outcome.failed_samples == 0


@pytest.mark.asyncio
async def test_charging_from_pv_only_is_reported_distinctly():
    """Charging from the array is fine, but it is NOT exploiting the free import —
    worth distinguishing so a sunny day does not mask a broken ForceCharge group."""
    cfg = fast_cfg()
    site = ScriptedSite([(60.0, -4.0, 6.0, 0.0)] * 8)
    outcome = await build(cfg, site, [GOOD]).run_window()
    assert any("from PV" in f for f in outcome.findings)


@pytest.mark.asyncio
async def test_bad_schedule_fails_the_window_even_when_charging():
    """Config and behaviour fail independently — a full battery would hide a
    deleted ForceCharge group until the first dull day."""
    cfg = fast_cfg()
    script = [(90 + i, -5.0, 0.5, 5.0) for i in range(6)] + [(100.0, 0.0, 0.0, 0.0)]
    outcome = await build(cfg, ScriptedSite(script), [{**GOOD, "enable": 0}]).run_window()
    assert not outcome.schedule_ok
    assert not outcome.ok


@pytest.mark.asyncio
async def test_missing_schedule_reader_does_not_fail_the_audit():
    """A controller that cannot read its schedule falls back to behaviour only."""
    cfg = fast_cfg()
    outcome = await build(cfg, ScriptedSite([(100.0, 0.0, 0.0, 0.0)] * 8), None).run_window()
    assert outcome.schedule_ok
    assert outcome.ok


@pytest.mark.asyncio
async def test_remediation_is_off_by_default_and_works_when_enabled():
    cfg = fast_cfg()
    site = ScriptedSite([(45.0, 0.0, 0.0, 0.0)] * 8)
    assert not (await build(cfg, site, [GOOD]).run_window()).remediated

    cfg.strategy.free_window_assurance.remediate = True
    outcome = await build(cfg, ScriptedSite([(45.0, 0.0, 0.0, 0.0)] * 8), [GOOD]).run_window()
    assert outcome.remediated


@pytest.mark.asyncio
async def test_assurance_api_cost_stays_small():
    """19 calls of the 1440 daily budget; the evening loop needs the rest."""
    cfg = AppConfig()
    a = cfg.strategy.free_window_assurance
    polls = 3 * 3600 // a.check_interval_seconds
    assert polls + 1 <= 25, f"{polls + 1} calls is too much of the 1440/day allowance"


def test_unclaimed_energy_is_priced_at_what_it_will_cost_instead():
    cfg = AppConfig()
    rate = cfg.plan.tariff.blended_import_rate(cfg.plan.credit_window_end, 14.0)
    assert rate > 0.4  # the overnight blend, not the $0.10 export rate


# ------------------------------------------------- charge-rate-limited windows
@pytest.mark.asyncio
async def test_depleted_start_is_judged_against_physics_not_a_flat_threshold():
    """Observed live: 17% SOC at 10:47. Three hours at 10 kW adds ~28.5 kWh into a
    47 kWh pack = ~61 points, so ~78% is the ceiling. Scoring against a flat 95%
    would cry wolf on exactly the days the owner most needs to trust the alert."""
    cfg = fast_cfg()
    script = [(17 + i * 12, -9.5, 0.0, 9.5) for i in range(6)] + [(78.0, -9.0, 0.0, 9.0)]
    outcome = await build(cfg, ScriptedSite(script), [GOOD]).run_window()

    assert outcome.achievable_soc_pct == pytest.approx(17 + 28.5 / 47 * 100, abs=1.0)
    assert outcome.final_soc_pct < cfg.strategy.free_window_assurance.alert_soc_pct
    assert outcome.ok, "a charge-rate-limited window that did its best is not a failure"
    assert any("charge-rate limited" in f for f in outcome.findings)


@pytest.mark.asyncio
async def test_depleted_start_that_underperforms_is_still_caught():
    """Physics forgives an unreachable 95%; it does not forgive idling at 30%."""
    cfg = fast_cfg()
    outcome = await build(cfg, ScriptedSite([(17.0, 0.0, 0.0, 0.0)] * 8), [GOOD]).run_window()
    assert not outcome.ok
    assert outcome.failed_samples > 0


@pytest.mark.asyncio
async def test_full_start_still_held_to_the_high_bar():
    cfg = fast_cfg()
    script = [(90.0, 0.0, 0.0, 0.0)] * 8
    outcome = await build(cfg, ScriptedSite(script), [GOOD]).run_window()
    assert outcome.achievable_soc_pct == pytest.approx(100.0)
    assert not outcome.ok, "90% with 10 kW of charge available and idle is a real failure"


@pytest.mark.asyncio
async def test_startup_grace_absorbs_a_late_starting_group():
    """Observed live: a group set to 11:01 has not engaged at 11:00:01. Counting
    that as a failure would raise an alert every single day, which trains the
    owner to ignore the one day it matters."""
    cfg = fast_cfg()
    cfg.strategy.free_window_assurance.startup_grace_minutes = 45
    cfg.strategy.free_window_assurance.check_interval_seconds = 1800
    # Idle for the first half hour, then charging hard to full.
    script = [(15.0, 0.0, 0.0, 0.0)] + [(min(100.0, 40.0 + i * 15) , -9.8, 0.0, 9.8) for i in range(5)] + [(100.0, 0.0, 0.0, 0.0)]
    outcome = await build(cfg, ScriptedSite(script), [GOOD]).run_window()
    assert outcome.failed_samples == 0, "the pre-start reading should not count"
    assert outcome.ok


@pytest.mark.asyncio
async def test_grace_period_does_not_hide_a_window_that_never_starts():
    cfg = fast_cfg()
    cfg.strategy.free_window_assurance.startup_grace_minutes = 6
    outcome = await build(cfg, ScriptedSite([(15.0, 0.0, 0.0, 0.0)] * 10), [GOOD]).run_window()
    assert not outcome.ok
    assert outcome.failed_samples > 0


@pytest.mark.asyncio
async def test_a_window_we_could_not_observe_is_not_reported_as_healthy():
    """A revoked key or an exhausted quota must not read as a clean window. The
    honest answer is 'could not check', and it has to be loud while the window is
    still open — by the 14:00 summary the free energy is gone."""
    from zerohero_dynamic_control.data_providers.base import ProviderError, TelemetryProvider

    class DeadProvider(TelemetryProvider):
        name = "dead"

        async def read(self, now):
            raise ProviderError("40256 illegal signature")

    cfg = fast_cfg()
    outcome = await build(cfg, DeadProvider(), [GOOD]).run_window()
    assert outcome.samples == 0
    assert not outcome.ok, "an unobserved window must never report OK"
    assert any("could not check" in f for f in outcome.findings)
    assert any("telemetry unavailable" in f for f in outcome.findings)
