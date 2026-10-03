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
from zerohero_dynamic_control.models import BatteryMode, ForecastPoint, Telemetry, soc_to_energy
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


def test_stale_telemetry_never_lowers_the_setpoint():
    """Seen live at 18:55: a stale sample cut 4.75 kW to the 3.0 kW fallback while
    the house was still drawing 4 kW. No new information is no reason to back off."""
    cfg = AppConfig()
    now = datetime(2026, 9, 27, 18, 55, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), now)
    runner.last_setpoint_kw = 4.75
    stale = Telemetry(
        timestamp=now, soc_pct=55.0, battery_energy_kwh=soc_to_energy(55.0, 47.0),
        solar_kw=0.0, load_kw=4.0, battery_kw=2.1, grid_kw=1.9, stale=True,
    )
    setpoint, reason = runner.compute_setpoint(stale, now)
    assert setpoint == pytest.approx(4.75)
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


# ------------------------------------------------- bad data from the vendor cloud
class DriftingSite(TelemetryProvider):
    """Replays a scripted SOC sequence, including a glitch."""

    name = "drifting"

    def __init__(self, socs):
        self.socs = list(socs)
        self.i = 0

    async def read(self, now):
        soc = self.socs[min(self.i, len(self.socs) - 1)]
        self.i += 1
        return Telemetry(
            timestamp=now, soc_pct=soc, battery_energy_kwh=soc_to_energy(soc, 47.0),
            solar_kw=0.0, load_kw=3.0, battery_kw=-9.9, grid_kw=12.9,
        )


@pytest.mark.asyncio
async def test_impossible_soc_drop_is_rejected():
    """Seen live on a FoxESS cloud feed: a stale snapshot reported 35% while the
    pack was really at 44% and charging. A 10 kW inverter into 47 kWh moves SOC by
    ~0.35 points/min, so a 9-point drop in 2 minutes cannot be real. Believing it
    at 17:50 would abandon a winnable $1 credit."""
    rate = 10.0 / 47.0 * 100.0 / 60.0
    cache = CachingTelemetryProvider(DriftingSite([44.0, 35.0, 35.0, 56.0]),
                                     max_soc_rate_pct_per_min=rate)
    t0 = datetime(2026, 9, 27, 12, 24, tzinfo=TZ)
    assert (await cache.read(t0)).soc_pct == 44.0
    r1 = await cache.read(t0 + timedelta(minutes=2))
    assert r1.soc_pct == 44.0 and r1.stale, "the impossible 35% should have been discarded"
    assert cache.rejected_samples == 1


@pytest.mark.asyncio
async def test_plausible_charging_is_not_rejected():
    """~0.35 points/min is normal at full charge and must pass untouched."""
    rate = 10.0 / 47.0 * 100.0 / 60.0
    cache = CachingTelemetryProvider(DriftingSite([44.0, 47.5, 51.0]),
                                     max_soc_rate_pct_per_min=rate)
    t0 = datetime(2026, 9, 27, 12, 24, tzinfo=TZ)
    await cache.read(t0)
    assert (await cache.read(t0 + timedelta(minutes=10))).soc_pct == 47.5
    assert cache.rejected_samples == 0


@pytest.mark.asyncio
async def test_one_point_soc_tick_is_not_rejected():
    """FoxESS reports whole points, so 56% -> 55% a minute apart is an ordinary
    tick, not a 1 point/min discharge. Rejecting it 19 times on the first live
    evening discarded the fresh snapshot each time, including the one at 18:50
    that showed a 4 kW load spike."""
    rate = 10.0 / 47.0 * 100.0 / 60.0
    cache = CachingTelemetryProvider(DriftingSite([56.0, 55.0, 54.0]),
                                     max_soc_rate_pct_per_min=rate)
    t0 = datetime(2026, 9, 27, 18, 49, tzinfo=TZ)
    await cache.read(t0)
    r1 = await cache.read(t0 + timedelta(minutes=1))
    assert r1.soc_pct == 55.0 and not r1.stale
    r2 = await cache.read(t0 + timedelta(minutes=2))
    assert r2.soc_pct == 54.0 and not r2.stale
    assert cache.rejected_samples == 0


class CloudSnapshots(TelemetryProvider):
    """The FoxESS cloud: polled every minute, a new snapshot only every five."""

    name = "cloud"

    def __init__(self, t0, socs):
        self.t0, self.socs = t0, socs

    async def read(self, now):
        i = min(int((now - self.t0).total_seconds() // 300), len(self.socs) - 1)
        soc = self.socs[i]
        return Telemetry(timestamp=now, soc_pct=soc, battery_energy_kwh=soc_to_energy(soc, 47.0),
                         load_kw=1.8 + 0.01 * i, battery_kw=9.2, grid_kw=-7.4 - 0.01 * i)


@pytest.mark.asyncio
async def test_fast_discharge_through_cloud_snapshots_is_not_rejected():
    """28 Sep, 18:05-18:41: exporting at 9.2 kW, the cloud delivered SOC in 2-point
    steps five minutes apart. Measured from the previous POLL a minute earlier that
    is 2 points a minute, and every fresh snapshot was rejected once. Measured from
    when the previous snapshot appeared, it is 0.4 a minute: entirely plausible."""
    rate = 10.0 / 47.0 * 100.0 / 60.0
    t0 = datetime(2026, 9, 28, 18, 5, tzinfo=TZ)
    cache = CachingTelemetryProvider(CloudSnapshots(t0, [96, 94, 92, 90, 88, 86, 84, 82]),
                                     max_soc_rate_pct_per_min=rate)
    for minute in range(0, 40):
        r = await cache.read(t0 + timedelta(minutes=minute))
        assert not r.stale, f"rejected at +{minute} min"
    assert cache.rejected_samples == 0


@pytest.mark.asyncio
async def test_persistent_disagreement_eventually_wins():
    """If the 'impossible' value keeps coming back, our baseline is the wrong one.
    Refusing forever would leave the controller steering on a fossil."""
    rate = 10.0 / 47.0 * 100.0 / 60.0
    cache = CachingTelemetryProvider(DriftingSite([80.0] + [20.0] * 6),
                                     max_soc_rate_pct_per_min=rate, max_rejects=3)
    t = datetime(2026, 9, 27, 12, 0, tzinfo=TZ)
    await cache.read(t)
    for i in range(1, 5):
        r = await cache.read(t + timedelta(minutes=i))
    assert r.soc_pct == 20.0, "should have conceded after max_rejects"


@pytest.mark.asyncio
async def test_long_gap_allows_a_large_change():
    """After a restart or outage the battery really can have moved a long way."""
    rate = 10.0 / 47.0 * 100.0 / 60.0
    cache = CachingTelemetryProvider(DriftingSite([80.0, 20.0]),
                                     max_soc_rate_pct_per_min=rate)
    t = datetime(2026, 9, 27, 12, 0, tzinfo=TZ)
    await cache.read(t)
    assert (await cache.read(t + timedelta(hours=4))).soc_pct == 20.0


@pytest.mark.asyncio
async def test_assumed_soc_is_flagged_not_silently_plausible():
    """The stand-in SOC is min_reserve+10 = 35%, which looks exactly like a real
    reading in the API output — it was misread as one during commissioning. The
    decision must say plainly that the battery was never measured."""
    cfg = AppConfig()
    now = datetime(2026, 9, 27, 17, 50, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(fail_after=0), BoomForecast(), now)
    runner.telemetry = CachingTelemetryProvider(BoomTelemetry(fail_after=0))

    decision = await runner.make_decision()
    assert decision.telemetry_assumed
    assert decision.degraded
    assert decision.starting_soc_pct == cfg.battery.min_reserve_soc_pct + 10.0
    assert any("TELEMETRY UNAVAILABLE" in r for r in decision.rationale)
    assert any("NOT measured" in r for r in decision.rationale)


@pytest.mark.asyncio
async def test_forecast_only_failure_is_not_labelled_as_telemetry_loss():
    """A dead forecast is a much smaller problem than a dead battery reading, and
    conflating them sent commissioning down the wrong path."""
    cfg = AppConfig()
    now = datetime(2026, 9, 27, 17, 50, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(fail_after=99), BoomForecast(), now)
    decision = await runner.make_decision()
    assert not decision.telemetry_assumed
    assert decision.degraded
    assert any("forecast unavailable" in r for r in decision.rationale)


# ----------------------------------------------------- solar array orientation
def test_open_meteo_azimuth_faces_the_sun_in_each_hemisphere():
    """Open-Meteo's azimuth is 0 = SOUTH with no hemisphere adjustment, so a
    hard-coded 0 models a south-facing array in Sydney — pointing away from the
    sun and under-forecasting badly. Below the equator north is 180."""
    from zerohero_dynamic_control.data_providers.open_meteo import OpenMeteoForecastProvider

    cfg = AppConfig()
    cfg.site.latitude = -33.7                      # Sydney
    assert OpenMeteoForecastProvider(cfg)._azimuth() == 180.0

    cfg.site.latitude = 51.5                       # London
    assert OpenMeteoForecastProvider(cfg)._azimuth() == 0.0


def test_explicit_azimuth_overrides_the_hemisphere_default():
    """An east/west split array is not simply 'equator-facing'."""
    from zerohero_dynamic_control.data_providers.open_meteo import OpenMeteoForecastProvider

    cfg = AppConfig()
    cfg.site.latitude = -33.7
    cfg.forecast.array_azimuth_deg = -90.0         # east-facing
    assert OpenMeteoForecastProvider(cfg)._azimuth() == -90.0


# ------------------------------------------------------- restarts mid-window
def _ledger(tmp_path):
    from zerohero_dynamic_control.ledger import Ledger

    return Ledger(tmp_path / "ledger.jsonl", tmp_path / "decisions.jsonl", tmp_path / "samples.jsonl")


def _sample(ledger, when, grid_kw):
    ledger.record_sample(
        Telemetry(timestamp=when, soc_pct=55.0, battery_energy_kwh=soc_to_energy(55.0, 47.0),
                  load_kw=2.0, grid_kw=grid_kw),
        setpoint_kw=2.0,
    )


@pytest.mark.asyncio
async def test_restart_mid_window_remembers_an_earlier_breach(tmp_path):
    """A fresh monitor after a restart used to forget the hour it had already lost,
    and would have reported the day clean. The samples log remembers."""
    cfg = AppConfig()
    ledger = _ledger(tmp_path)
    start = datetime(2026, 9, 27, 18, 0, tzinfo=TZ)
    for m in range(0, 30):
        _sample(ledger, start + timedelta(minutes=m), 1.9 if 10 <= m < 15 else -0.3)
    now = start + timedelta(minutes=31)
    runner = build_runner(cfg, BoomTelemetry(fail_after=99), BoomForecast(), now)
    runner.ledger = ledger

    await runner.make_decision()
    assert runner.monitor is not None
    assert [b.hour_start.hour for b in runner.monitor.breached_hours()] == [18]
    assert runner.exported_kwh > 0


@pytest.mark.asyncio
async def test_partial_close_out_does_not_claim_the_credit(tmp_path):
    """The shutdown path closes the window early. It must not write a pass for
    hours it never reached, and a later full row must supersede it."""
    from zerohero_dynamic_control.ledger import verdict_of

    cfg = AppConfig()
    ledger = _ledger(tmp_path)
    now = datetime(2026, 9, 27, 18, 5, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(fail_after=99), BoomForecast(), now)
    runner.ledger = ledger
    await runner.make_decision()
    early = await runner.close_out()
    assert early.partial and not early.credit_secured
    assert verdict_of(early) == "UNVERIFIED"

    ledger.record_outcome(early.model_copy(update={"partial": False, "exported_kwh": 1.0}))
    rows = ledger.read_outcomes()
    assert len(rows) == 1 and rows[0].exported_kwh == 1.0


# ------------------------------------------------------- write pacing
class LoadScript(TelemetryProvider):
    """Returns whatever house load the test sets, with the battery covering it."""

    name = "script"

    def __init__(self):
        self.load_kw = 2.0

    async def read(self, now):
        return Telemetry(timestamp=now, soc_pct=60.0, battery_energy_kwh=soc_to_energy(60.0, 47.0),
                         load_kw=self.load_kw, battery_kw=self.load_kw, grid_kw=0.0)


@pytest.mark.asyncio
async def test_raises_go_out_at_once_and_lowers_wait():
    """With 10 s local telemetry every flicker of load is visible. Raising answers
    import and cannot wait; lowering only trims export, so it is paced to save
    cloud writes."""
    cfg = AppConfig()
    cfg.controller.min_lower_interval_seconds = 30
    t0 = datetime(2026, 9, 28, 18, 30, tzinfo=TZ)
    site = LoadScript()
    runner = build_runner(cfg, site, BoomForecast(), t0)
    runner.choose_mode = lambda tel, now: (BatteryMode.FORCE_EXPORT, "exporting")

    await runner.tick(t0)
    first = runner.controller.last_command.power_kw

    site.load_kw = 1.0
    await runner.tick(t0 + timedelta(seconds=10))
    assert runner.controller.last_command.power_kw == first, "lower held back inside 30 s"

    site.load_kw = 4.0
    await runner.tick(t0 + timedelta(seconds=20))
    assert runner.controller.last_command.power_kw > first, "a raise is never held back"
    raised = runner.controller.last_command.power_kw

    site.load_kw = 1.0
    await runner.tick(t0 + timedelta(seconds=30))
    assert runner.controller.last_command.power_kw == raised
    await runner.tick(t0 + timedelta(seconds=55))
    assert runner.controller.last_command.power_kw < raised, "lowered once the interval passed"


@pytest.mark.asyncio
async def test_no_pacing_by_default():
    cfg = AppConfig()
    t0 = datetime(2026, 9, 28, 18, 30, tzinfo=TZ)
    site = LoadScript()
    runner = build_runner(cfg, site, BoomForecast(), t0)
    runner.choose_mode = lambda tel, now: (BatteryMode.FORCE_EXPORT, "exporting")
    await runner.tick(t0)
    first = runner.controller.last_command.power_kw
    site.load_kw = 1.0
    await runner.tick(t0 + timedelta(seconds=60))
    assert runner.controller.last_command.power_kw < first


# ------------------------------------------------- self-use unless exporting
def _decision_with_export(cfg, start, export_kw_by_minute):
    """A decision whose slots export the given kW from each minute offset onward."""
    from zerohero_dynamic_control.models import Decision, SlotPlan

    slots = []
    for m in range(0, 180, 5):
        kw = 0.0
        for at, v in sorted(export_kw_by_minute.items()):
            if m >= at:
                kw = v
        slots.append(SlotPlan(start=start + timedelta(minutes=m), end=start + timedelta(minutes=m + 5),
                              solar_kw=0.0, load_kw=2.0, mandatory_discharge_kw=2.25,
                              export_discharge_kw=kw, export_headroom_kw=7.75))
    return Decision(
        made_at=start - timedelta(minutes=10), window_start=start, window_end=start + timedelta(hours=3),
        starting_soc_pct=90.0, starting_energy_kwh=soc_to_energy(90.0, 47.0), target_export_kwh=0,
        mandatory_discharge_kwh=6.75, opportunistic_export_kwh=0, passive_solar_export_kwh=0,
        recommended_discharge_kw=2.25, peak_discharge_kw=10, expected_final_soc=50,
        expected_final_energy_kwh=20, battery_dc_drawn_kwh=10, reserve_floor_soc_pct=25,
        overnight_retention_kwh=15, energy_shortfall_kwh=0, expected_net_aud=0,
        slots=slots, credit_achievable=True, recommended_mode=BatteryMode.FORCE_EXPORT,
    )


def test_force_discharge_only_while_the_export_is_a_real_buffer():
    cfg = AppConfig()
    start = datetime(2026, 9, 28, 18, 0, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), start)
    runner.decision = _decision_with_export(cfg, start, {0: 7.5, 40: 1.0, 60: 0.0})
    tel = Telemetry(timestamp=start, soc_pct=90.0, battery_energy_kwh=soc_to_energy(90.0, 47.0),
                    load_kw=2.0, battery_kw=2.0, grid_kw=0.0)
    assert runner.choose_mode(tel, start + timedelta(minutes=10))[0] is BatteryMode.FORCE_EXPORT
    assert runner.choose_mode(tel, start + timedelta(minutes=45))[0] is BatteryMode.SELF_CONSUMPTION, \
        "1 kW of export is no buffer against a kettle"
    assert runner.choose_mode(tel, start + timedelta(minutes=90))[0] is BatteryMode.SELF_CONSUMPTION


def test_no_plan_or_an_abandoned_credit_means_self_use():
    cfg = AppConfig()
    start = datetime(2026, 9, 28, 18, 0, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), start)
    assert runner.choose_mode(None, start)[0] is BatteryMode.SELF_CONSUMPTION
    runner.decision = _decision_with_export(cfg, start, {0: 7.5})
    runner.decision.recommended_mode = BatteryMode.SELF_CONSUMPTION
    assert runner.choose_mode(None, start)[0] is BatteryMode.SELF_CONSUMPTION
    runner.decision.recommended_mode = BatteryMode.FORCE_EXPORT
    runner.manual_override_kw = 4.0
    runner.decision.slots = []
    assert runner.choose_mode(None, start)[0] is BatteryMode.FORCE_EXPORT, "an override always forces"


class BoundedPrinter(PrintingBatteryController):
    def capabilities(self):
        from dataclasses import replace

        return replace(super().capabilities(), window_bounded=True)


@pytest.mark.asyncio
async def test_window_bounded_controllers_are_prearmed_at_decision_time():
    """FoxESS groups are bounded to 18:00-20:59, so writing at 17:50 changes nothing
    before 18:00 and removes the gap while the first 18:00 write is in flight."""
    cfg = AppConfig()
    t = datetime(2026, 9, 28, 17, 50, tzinfo=TZ)
    start = datetime(2026, 9, 28, 18, 0, tzinfo=TZ)
    for inner, expect in ((BoundedPrinter(quiet=True), True), (PrintingBatteryController(quiet=True), False)):
        runner = EveningRunner(cfg, telemetry=BoomTelemetry(fail_after=99), forecast=BoomForecast(),
                               controller=inner, ledger=None, clock=SimClock(t))
        runner.decision = _decision_with_export(cfg, start, {0: 7.5})
        runner.decision.window_end = start + timedelta(minutes=1)
        await runner.run_window()
        early = [c for c in inner.command_log if c.timestamp < start]
        assert bool(early) is expect
        if expect:
            assert early[0].mode is BatteryMode.FORCE_EXPORT and early[0].power_kw == pytest.approx(9.75)



def test_the_mode_at_a_reading_comes_from_the_command_log():
    """A reading is judged by the mode we had commanded when it was MEASURED."""
    from zerohero_dynamic_control.models import ControlCommand

    cfg = AppConfig()
    t = datetime(2026, 10, 1, 18, 0, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), t)
    assert runner._mode_at(t) is None, "nothing commanded yet"
    runner.controller.command_log.append(
        ControlCommand(timestamp=t - timedelta(minutes=10), mode=BatteryMode.FORCE_EXPORT, power_kw=10))
    runner.controller.command_log.append(
        ControlCommand(timestamp=t + timedelta(minutes=44), mode=BatteryMode.SELF_CONSUMPTION))
    assert runner._mode_at(t + timedelta(minutes=40)) is BatteryMode.FORCE_EXPORT
    assert runner._mode_at(t + timedelta(minutes=49, seconds=50)) is BatteryMode.SELF_CONSUMPTION


def test_export_counts_from_18_when_the_first_reading_arrives_at_18_05():
    """2 Oct: the controller counted 2.1 kWh sold, GloBird 3.0. The first in-window
    snapshot was measured at 18:04:50; the battery had been exporting since 18:00."""
    cfg = AppConfig()
    start = datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
    runner = build_runner(cfg, BoomTelemetry(), BoomForecast(), start)
    runner.decision = _decision_with_export(cfg, start, {0: 7.5})
    runner._account(start - timedelta(seconds=10), 0.03, forced=False)      # 17:59:50, ignored
    runner._account(start + timedelta(minutes=4, seconds=50), -7.8)
    assert runner.exported_kwh == pytest.approx(7.8 * (4 + 50 / 60) / 60)
