"""FoxESS integration, exercised against a fake transport — no network, no key."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest

from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.controllers.foxess import FoxESSController
from zerohero_dynamic_control.data_providers.foxess import (
    RESIDUAL_ENERGY_SCALE,
    FoxESSTelemetryProvider,
)
from zerohero_dynamic_control.foxess_client import (
    CallBudget,
    FoxESSClient,
    FoxESSError,
    FoxESSQuotaExhausted,
)

from .conftest import TZ


class FakeFoxESS(FoxESSClient):
    """Records calls and replays canned responses instead of hitting the cloud."""

    def __init__(self, responses: dict | None = None, **kw):
        # Zero the throttles: the real 1 s/2 s vendor limits are verified in
        # test_capabilities_report_a_true_power_setpoint, not re-slept in every test.
        kw.setdefault("query_interval", 0.0)
        kw.setdefault("update_interval", 0.0)
        super().__init__("FAKEKEY", **kw)
        self.calls: list[tuple[str, dict | None]] = []
        self.responses = responses or {}
        self.fail_paths: set[str] = set()

    async def _send(self, path, body, method):  # type: ignore[override]
        self.calls.append((path, body))
        if path in self.fail_paths:
            raise FoxESSError(f"simulated failure on {path}")
        return {"errno": 0, "result": self.responses.get(path, {})}


# --------------------------------------------------------------------- signing
def test_signature_uses_literal_backslash_escapes():
    """The FoxESS docs say url + \\r\\n + token + \\r\\n + timestamp, and they mean the
    LITERAL four characters — not a real CRLF. Getting this wrong is the single most
    common cause of unexplained auth failures."""
    sig = FoxESSClient._signature("/op/v0/device/real/query", "KEY", "1700000000000")
    literal = hashlib.md5(rb"/op/v0/device/real/query\r\nKEY\r\n1700000000000").hexdigest()
    real_crlf = hashlib.md5(b"/op/v0/device/real/query\r\nKEY\r\n1700000000000").hexdigest()
    assert sig == literal
    assert sig != real_crlf


def test_headers_carry_token_timestamp_and_signature():
    client = FoxESSClient("KEY")
    h = client._headers("/op/v0/device/list")
    assert h["token"] == "KEY"
    assert h["timestamp"].isdigit() and len(h["timestamp"]) == 13  # milliseconds
    assert h["signature"] == FoxESSClient._signature("/op/v0/device/list", "KEY", h["timestamp"])


def test_empty_api_key_is_rejected_up_front():
    with pytest.raises(FoxESSError, match="API key"):
        FoxESSClient("")


# ---------------------------------------------------------------- call budget
def test_budget_blocks_routine_calls_near_the_limit():
    """1440/day is a hard vendor limit. Exhausting it would lock us out of our own
    inverter for the rest of the day, so routine polling must stop at the reserve."""
    budget = CallBudget(daily_limit=1440, reserve=120)
    now = datetime(2026, 1, 15, 18, 0)
    budget._roll(now)
    budget.used = 1440 - 120
    with pytest.raises(FoxESSQuotaExhausted):
        budget.check(now, path="/op/v0/device/real/query", critical=False)


def test_budget_always_allows_critical_calls():
    """Close-out and baseline restore must run even with the budget spent — leaving
    the inverter in ForceDischarge overnight is far worse than one extra call."""
    budget = CallBudget(daily_limit=1440, reserve=120)
    now = datetime(2026, 1, 15, 21, 0)
    budget._roll(now)
    budget.used = 1439
    budget.check(now, path="/op/v0/device/scheduler/set/flag", critical=True)


def test_budget_rolls_over_at_midnight():
    budget = CallBudget()
    budget.check(datetime(2026, 1, 15, 23, 0), path="/x", critical=False)
    budget.spend("/x")
    assert budget.remaining(datetime(2026, 1, 15, 23, 30)) == 1439
    assert budget.remaining(datetime(2026, 1, 16, 0, 1)) == 1440


@pytest.mark.asyncio
async def test_a_full_day_of_control_fits_inside_the_quota():
    """Two 3-hour windows at 60 s plus writes must stay well under 1440."""
    cfg = AppConfig()
    polls_per_window = 3 * 3600 // cfg.strategy.control_interval_seconds
    reads = polls_per_window * 2                 # credit window + free charge window
    writes = polls_per_window + 4                # worst case: a write every tick
    assert reads + writes < 1440, f"{reads + writes} calls/day exceeds the FoxESS limit"


@pytest.mark.asyncio
async def test_errno_is_surfaced_as_an_error():
    client = FakeFoxESS()

    async def _send(path, body, method):
        return {"errno": 40256, "msg": "signature error"}

    client._send = _send  # type: ignore[assignment]
    with pytest.raises(FoxESSError, match="40256"):
        await client.request("/op/v0/device/list", {})


# ------------------------------------------------------------------ telemetry
@pytest.fixture
def raw_reading():
    return {
        "SoC": 82.0,
        "ResidualEnergy": 3854.0,       # x0.01 -> 38.54 kWh
        "pvPower": 2.4,
        "loadsPower": 3.1,
        "gridConsumptionPower": 0.0,
        "feedinPower": 1.8,
        "batDischargePower": 2.5,
        "batChargePower": 0.0,
    }


def test_telemetry_normalises_foxess_signs(raw_reading):
    """FoxESS splits grid flow into two non-negative variables; we use one signed
    figure where positive means importing."""
    cfg = AppConfig()
    provider = FoxESSTelemetryProvider(cfg, FakeFoxESS(), "SN123")
    now = datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    tel = provider.to_telemetry(raw_reading, now)

    assert tel.soc_pct == 82.0
    assert tel.battery_energy_kwh == pytest.approx(3854.0 * RESIDUAL_ENERGY_SCALE)
    assert tel.grid_kw == pytest.approx(-1.8)   # exporting
    assert tel.export_kw == pytest.approx(1.8)
    assert tel.import_kw == 0.0
    assert tel.battery_kw == pytest.approx(2.5)  # discharging
    assert tel.net_load_kw == pytest.approx(3.1 - 2.4)


def test_telemetry_prefers_the_unambiguous_power_pair(raw_reading):
    """invBatPower's sign convention has moved across firmware revisions, so the
    explicit charge/discharge pair wins whenever it is present."""
    cfg = AppConfig()
    provider = FoxESSTelemetryProvider(cfg, FakeFoxESS(), "SN")
    raw = {**raw_reading, "invBatPower": -99.0}
    tel = provider.to_telemetry(raw, datetime(2026, 1, 15, 18, 30, tzinfo=TZ))
    assert tel.battery_kw == pytest.approx(2.5)


def test_telemetry_falls_back_to_signed_invbatpower(raw_reading):
    cfg = AppConfig()
    provider = FoxESSTelemetryProvider(cfg, FakeFoxESS(), "SN")
    raw = {k: v for k, v in raw_reading.items() if not k.startswith("bat")}
    raw["invBatPower"] = 2.5
    tel = provider.to_telemetry(raw, datetime(2026, 1, 15, 18, 30, tzinfo=TZ))
    assert tel.battery_kw == pytest.approx(2.5)

    cfg.providers.foxess.invert_battery_power_sign = True
    assert provider.to_telemetry(raw, datetime(2026, 1, 15, 18, 30, tzinfo=TZ)).battery_kw == pytest.approx(-2.5)


def test_telemetry_falls_back_to_soc_when_residual_energy_missing(raw_reading):
    cfg = AppConfig()
    provider = FoxESSTelemetryProvider(cfg, FakeFoxESS(), "SN")
    raw = {k: v for k, v in raw_reading.items() if k != "ResidualEnergy"}
    tel = provider.to_telemetry(raw, datetime(2026, 1, 15, 18, 30, tzinfo=TZ))
    assert tel.battery_energy_kwh == pytest.approx(0.82 * cfg.battery.usable_capacity_kwh)


@pytest.mark.asyncio
async def test_one_poll_is_one_api_call():
    """Batching every variable into a single request is what keeps a 3-hour window
    at 180 calls rather than 900."""
    from zerohero_dynamic_control.data_providers.foxess import VARIABLES

    client = FakeFoxESS(responses={"/op/v0/device/real/query": [
        {"variable": "SoC", "value": 80.0}, {"variable": "loadsPower", "value": 2.0},
    ]})
    provider = FoxESSTelemetryProvider(AppConfig(), client, "SN")
    await provider.read(datetime(2026, 1, 15, 18, 0, tzinfo=TZ))
    assert len(client.calls) == 1
    assert len(client.calls[0][1]["variables"]) == len(VARIABLES)


# ----------------------------------------------------------------- controller
def build_controller(client: FakeFoxESS, cfg: AppConfig | None = None) -> FoxESSController:
    cfg = cfg or AppConfig()
    return FoxESSController(
        client, "SN123",
        window_start=cfg.plan.credit_window_start,
        window_end=cfg.plan.credit_window_end,
        min_soc_on_grid_pct=int(cfg.battery.emergency_floor_soc_pct),
        max_power_kw=cfg.inverter.ac_limit_kw,
    )


@pytest.mark.asyncio
async def test_force_export_sends_watts_not_kilowatts():
    """fdPwr is an integer number of WATTS. A kW value here would discharge the
    battery a thousand times slower than intended."""
    from zerohero_dynamic_control.models import BatteryMode

    client = FakeFoxESS()
    ctl = build_controller(client)
    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    await ctl.set_power(4.25, now=now)

    write = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1]
    group = write[1]["groups"][0]
    assert group["fdPwr"] == 4250
    assert group["workMode"] == "ForceDischarge"


@pytest.mark.asyncio
async def test_hardware_floor_is_set_on_every_command():
    """fdSoc puts the floor inside the inverter, so a crashed controller or a dead
    network cannot flatten the battery."""
    cfg = AppConfig()
    client = FakeFoxESS()
    ctl = build_controller(client, cfg)
    from zerohero_dynamic_control.models import BatteryMode

    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    group = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"][0]
    assert group["fdSoc"] == int(cfg.battery.emergency_floor_soc_pct)
    assert group["minSocOnGrid"] == int(cfg.battery.emergency_floor_soc_pct)


@pytest.mark.asyncio
async def test_power_is_clamped_to_the_inverter_limit():
    client = FakeFoxESS()
    ctl = build_controller(client)
    from zerohero_dynamic_control.models import BatteryMode

    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    await ctl.set_power(99.0, now=now)
    group = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"][0]
    assert group["fdPwr"] == 10000


@pytest.mark.asyncio
async def test_existing_schedule_is_saved_and_restored():
    """scheduler/enable replaces the WHOLE group list, so without this the owner's
    own schedule would be silently destroyed."""
    baseline = [{"enable": 1, "startHour": 2, "startMinute": 0,
                "endHour": 4, "endMinute": 59, "workMode": "ForceCharge"}]
    client = FakeFoxESS(responses={"/op/v0/device/scheduler/get": {"groups": baseline}})
    ctl = build_controller(client)
    from zerohero_dynamic_control.models import BatteryMode

    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    await ctl.restore(now=now + timedelta(hours=3), reason="window closed")

    last = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1]
    assert last[1]["groups"] == baseline


@pytest.mark.asyncio
async def test_disables_scheduler_when_no_baseline_could_be_read():
    client = FakeFoxESS()
    client.fail_paths.add("/op/v0/device/scheduler/get")
    ctl = build_controller(client)
    await ctl.restore(now=datetime(2026, 1, 15, 21, 0, tzinfo=TZ), reason="close")
    assert any("/scheduler/set/flag" in c[0] for c in client.calls)


@pytest.mark.asyncio
async def test_self_consumption_restores_rather_than_writing_a_group():
    """Returning to normal means handing the inverter back, not pinning it to
    a SelfUse group that would outlive our window."""
    baseline = [{"enable": 1, "startHour": 2, "startMinute": 0,
                "endHour": 4, "endMinute": 59, "workMode": "ForceCharge"}]
    client = FakeFoxESS(responses={"/op/v0/device/scheduler/get": {"groups": baseline}})
    ctl = build_controller(client)
    from zerohero_dynamic_control.models import BatteryMode

    now = datetime(2026, 1, 15, 21, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    await ctl.set_mode(BatteryMode.SELF_CONSUMPTION, now=now)
    assert [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"] == baseline


@pytest.mark.asyncio
async def test_quota_exhaustion_skips_a_write_instead_of_aborting():
    """A missed setpoint nudge costs a few cents; aborting the window costs $1."""
    client = FakeFoxESS(budget=CallBudget(daily_limit=10, reserve=9))
    ctl = build_controller(client)
    from zerohero_dynamic_control.models import BatteryMode

    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    client.budget._roll(now)
    client.budget.used = 10
    await ctl._push(BatteryMode.FORCE_EXPORT, 3.0)  # must not raise


@pytest.mark.asyncio
async def test_group_window_matches_the_credit_window():
    cfg = AppConfig()
    client = FakeFoxESS()
    ctl = build_controller(client, cfg)
    from zerohero_dynamic_control.models import BatteryMode

    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 1, 15, 18, 0, tzinfo=TZ))
    group = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"][0]
    assert (group["startHour"], group["startMinute"]) == (18, 0)
    # 21:00 exclusive -> the group must stop at 20:59, not run into the next hour.
    assert (group["endHour"], group["endMinute"]) == (20, 59)


@pytest.mark.asyncio
async def test_capabilities_report_a_true_power_setpoint():
    ctl = build_controller(FakeFoxESS())
    caps = ctl.capabilities()
    assert caps.supports_power_setpoint and caps.supports_soc_target
    assert caps.min_command_interval_seconds >= 2.0  # documented update limit


@pytest.mark.asyncio
async def test_mode_map_covers_every_battery_mode():
    from zerohero_dynamic_control.controllers.foxess import MODE_MAP
    from zerohero_dynamic_control.models import BatteryMode

    assert set(MODE_MAP) == set(BatteryMode)
    assert MODE_MAP[BatteryMode.FORCE_EXPORT] == "ForceDischarge"
    assert MODE_MAP[BatteryMode.FORCE_CHARGE] == "ForceCharge"


# ------------------------------------------- coexisting with the owner's schedule
FREE_CHARGE_GROUP = {
    "enable": 1, "startHour": 11, "startMinute": 0, "endHour": 13, "endMinute": 59,
    "workMode": "ForceCharge", "minSocOnGrid": 10, "fdSoc": 10, "fdPwr": 10000,
}


@pytest.mark.asyncio
async def test_owner_free_charge_group_survives_our_window():
    """The 11:00-14:00 force charge is worth ~40 kWh/day of $0.00 energy. Our
    18:00-21:00 group must coexist with it, not replace it — otherwise a failed
    close-out would cost a whole day of free charging."""
    from zerohero_dynamic_control.models import BatteryMode

    client = FakeFoxESS(responses={"/op/v0/device/scheduler/get": {"groups": [FREE_CHARGE_GROUP]}})
    ctl = build_controller(client)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 1, 15, 18, 0, tzinfo=TZ))

    groups = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"]
    assert FREE_CHARGE_GROUP in groups, "the owner's free-charge group was wiped"
    assert any(g["workMode"] == "ForceDischarge" for g in groups)


@pytest.mark.asyncio
async def test_overlapping_groups_are_set_aside_not_duplicated():
    """A pre-existing group covering 18:00-21:00 would fight ours, so it is dropped
    for the duration and restored at close-out."""
    from zerohero_dynamic_control.models import BatteryMode

    clashing = {"enable": 1, "startHour": 17, "startMinute": 0,
                "endHour": 22, "endMinute": 0, "workMode": "SelfUse"}
    client = FakeFoxESS(responses={"/op/v0/device/scheduler/get":
                                   {"groups": [FREE_CHARGE_GROUP, clashing]}})
    ctl = build_controller(client)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 1, 15, 18, 0, tzinfo=TZ))

    groups = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"]
    assert clashing not in groups
    assert FREE_CHARGE_GROUP in groups
    assert len(groups) == 2


@pytest.mark.asyncio
async def test_group_list_is_capped_at_the_vendor_maximum():
    from zerohero_dynamic_control.controllers.foxess import MAX_SCHEDULER_GROUPS
    from zerohero_dynamic_control.models import BatteryMode

    many = [{"enable": 1, "startHour": h, "startMinute": 0, "endHour": h, "endMinute": 59,
             "workMode": "SelfUse"} for h in range(0, 10)]
    client = FakeFoxESS(responses={"/op/v0/device/scheduler/get": {"groups": many}})
    ctl = build_controller(client)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 1, 15, 18, 0, tzinfo=TZ))

    groups = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"]
    assert len(groups) <= MAX_SCHEDULER_GROUPS
    assert any(g["workMode"] == "ForceDischarge" for g in groups), "ours must never be the one dropped"


@pytest.mark.asyncio
async def test_repeated_setpoints_do_not_accumulate_groups():
    """Every minute of the window issues a write; the group list must stay stable."""
    from zerohero_dynamic_control.models import BatteryMode

    client = FakeFoxESS(responses={"/op/v0/device/scheduler/get": {"groups": [FREE_CHARGE_GROUP]}})
    ctl = build_controller(client)
    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    for kw in (3.0, 4.0, 5.0):
        await ctl.set_power(kw, now=now)

    groups = [c for c in client.calls if c[0].endswith("/scheduler/enable")][-1][1]["groups"]
    assert len(groups) == 2


# ---------------------------------------------------- graceful shutdown
@pytest.mark.asyncio
async def test_shutdown_restores_the_inverter():
    """`docker stop` at 19:30 must not leave the battery in ForceDischarge
    overnight, exporting at $0.02 to be re-bought at $0.407 in the morning."""
    from zerohero_dynamic_control.config import AppConfig
    from zerohero_dynamic_control.models import BatteryMode
    from zerohero_dynamic_control.scheduler import ZeroHeroScheduler

    baseline = [FREE_CHARGE_GROUP]
    client = FakeFoxESS(responses={"/op/v0/device/scheduler/get": {"groups": baseline},
                                   "/op/v1/device/scheduler/get": {"groups": baseline}})
    ctl = build_controller(client)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 1, 15, 19, 30, tzinfo=TZ))
    assert any(g["workMode"] == "ForceDischarge"
               for g in [c for c in client.calls if c[0].endswith("/enable")][-1][1]["groups"])

    cfg = AppConfig()
    sched = ZeroHeroScheduler.__new__(ZeroHeroScheduler)
    sched.cfg, sched.controller = cfg, ctl
    sched.telemetry = sched.forecast = ctl
    sched.current_runner = None
    from zerohero_dynamic_control.clock import RealClock

    sched.clock = RealClock(cfg.site.tz)
    await sched.shutdown()

    final = [c for c in client.calls if c[0].endswith("/enable")][-1][1]["groups"]
    assert final == baseline, "the owner's schedule was not restored on shutdown"


@pytest.mark.asyncio
async def test_shutdown_survives_a_failing_controller():
    """Shutdown is best-effort: one broken step must not skip the others."""
    from zerohero_dynamic_control.clock import RealClock
    from zerohero_dynamic_control.config import AppConfig
    from zerohero_dynamic_control.scheduler import ZeroHeroScheduler

    client = FakeFoxESS()
    client.fail_paths.update({"/op/v0/device/scheduler/enable",
                              "/op/v0/device/scheduler/set/flag"})
    ctl = build_controller(client)
    cfg = AppConfig()
    sched = ZeroHeroScheduler.__new__(ZeroHeroScheduler)
    sched.cfg, sched.controller = cfg, ctl
    sched.telemetry = sched.forecast = ctl
    sched.current_runner = None
    sched.clock = RealClock(cfg.site.tz)
    await sched.shutdown()  # must not raise


# ------------------------------------------- the inverter must free itself
@pytest.mark.asyncio
async def test_written_group_always_self_terminates_inside_the_window():
    """The safety model does not depend on this process surviving. Whatever we
    write must end before 21:00 on its own, so a killed container, a crashed NAS
    or a dead network all end with the battery back under normal rules."""
    from zerohero_dynamic_control.models import BatteryMode

    client = FakeFoxESS()
    ctl = build_controller(client)
    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    for kw in (1.0, 5.0, 9.9):
        await ctl.set_power(kw, now=now)

    for call in [c for c in client.calls if c[0].endswith("/enable")]:
        for g in call[1]["groups"]:
            if g.get("workMode") != "ForceDischarge":
                continue
            end = g["endHour"] * 60 + g["endMinute"]
            assert end < 21 * 60, f"group runs to {g['endHour']}:{g['endMinute']}, past 21:00"
            assert end > g["startHour"] * 60 + g["startMinute"]


@pytest.mark.asyncio
async def test_unbounded_forced_group_is_refused():
    """A future edit that drops the end time must fail loudly, not silently
    export the battery flat every night."""
    from zerohero_dynamic_control.controllers.base import ControllerError

    ctl = build_controller(FakeFoxESS())
    with pytest.raises(ControllerError, match="unbounded"):
        ctl._assert_bounded({"workMode": "ForceDischarge", "startHour": 18, "startMinute": 0,
                             "endHour": 23, "endMinute": 59})


@pytest.mark.asyncio
async def test_deadman_floor_is_the_planning_reserve_not_the_hard_floor():
    """If we die at 19:30 the inverter keeps discharging at the last power until
    the group expires. fdSoc is where that stops, so it must leave enough to run
    the house overnight — not drain to the 10% emergency minimum."""
    from zerohero_dynamic_control.config import AppConfig
    from zerohero_dynamic_control.controllers import build_controller as factory
    from zerohero_dynamic_control.models import BatteryMode

    cfg = AppConfig()
    cfg.controller.type = "foxess"
    cfg.providers.foxess.api_key = "K"
    cfg.providers.foxess.serial_number = "SN"
    wrapper = factory(cfg)
    inner = wrapper.inner
    assert inner.fd_soc_pct == int(cfg.battery.min_reserve_soc_pct) == 25
    assert inner.min_soc_on_grid_pct == int(cfg.battery.emergency_floor_soc_pct) == 10

    inner.client = FakeFoxESS()
    await inner.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 1, 15, 18, 0, tzinfo=TZ))
    g = [c for c in inner.client.calls if c[0].endswith("/enable")][-1][1]["groups"][0]
    assert g["fdSoc"] == 25, "an unattended inverter would drain below the planning reserve"
    assert g["minSocOnGrid"] == 10


@pytest.mark.asyncio
async def test_soc_target_can_lower_the_deadman_but_never_below_the_hard_floor():
    from zerohero_dynamic_control.models import BatteryMode

    client = FakeFoxESS()
    ctl = build_controller(client)
    ctl.fd_soc_pct = 25
    now = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=now)
    await ctl.set_soc_target(5.0, now=now, reason="credit at risk")
    assert ctl.fd_soc_pct == ctl.min_soc_on_grid_pct == 10


# --------------------------------------------------- secrets from the environment
def test_missing_env_var_fails_by_name_not_vaguely(monkeypatch, tmp_path):
    """An unset key must say WHICH variable is missing. Left to fail later it
    surfaces as a FoxESS 40256 'illegal signature', which reads like a wrong key
    rather than an absent one and sends you debugging the signature code."""
    import pytest as _pytest

    from zerohero_dynamic_control.config import AppConfig

    monkeypatch.delenv("FOXESS_API_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  foxess:\n    api_key: ${FOXESS_API_KEY}\n")
    with _pytest.raises(Exception, match="FOXESS_API_KEY is not in the environment"):
        AppConfig.load(cfg)


def test_env_var_is_stripped_of_whitespace_and_quotes(monkeypatch, tmp_path):
    """A .env written as KEY="abc" or with a trailing newline must still work —
    the signature is an exact MD5, so one stray quote breaks every request."""
    from zerohero_dynamic_control.config import AppConfig

    monkeypatch.setenv("FOXESS_API_KEY", '  "abc-123"  ')
    cfg = tmp_path / "config.yaml"
    cfg.write_text("providers:\n  foxess:\n    api_key: ${FOXESS_API_KEY}\n")
    assert AppConfig.load(cfg).providers.foxess.api_key == "abc-123"


# --------------------------------------------------------- version-skew guard
def test_unset_env_var_is_refused_by_name(monkeypatch):
    """The common case: config references ${FOXESS_SERIAL} and nobody set it."""
    import pytest as _pytest

    from zerohero_dynamic_control.config import FoxESSConfig

    monkeypatch.delenv("FOXESS_SERIAL", raising=False)
    with _pytest.raises(Exception, match="FOXESS_SERIAL is not in the environment"):
        FoxESSConfig(api_key="k", serial_number="${FOXESS_SERIAL}")


def test_unexpanded_placeholder_never_reaches_the_api():
    """Backstop for version skew. config.yaml is bind-mounted while the code lives
    in the image, so a pull can hand new config to an old build — which happened:
    the literal '${FOXESS_SERIAL}' went out as the serial, FoxESS answered errno 0
    with an empty payload, and it surfaced four layers away as 'telemetry
    unavailable'. If a future field is ever missed by the expansion validator,
    this catches it instead of the vendor silently returning nothing."""
    import pytest as _pytest

    from zerohero_dynamic_control.config import FoxESSConfig

    cfg = FoxESSConfig.model_construct(api_key="k", serial_number="${SOMETHING}")
    with _pytest.raises(ValueError, match="newer than the running image"):
        cfg._reject_unexpanded_placeholders()


@pytest.mark.asyncio
async def test_empty_variable_response_names_the_serial():
    """FoxESS returns errno 0 with no data for an unknown serial, so the error has
    to say which serial was tried or it reads like a generic outage."""
    from zerohero_dynamic_control.data_providers.base import ProviderError
    from zerohero_dynamic_control.data_providers.foxess import FoxESSTelemetryProvider

    client = FakeFoxESS(responses={"/op/v0/device/real/query": []})
    provider = FoxESSTelemetryProvider(AppConfig(), client, "WRONG-SN")
    with pytest.raises(ProviderError, match="WRONG-SN"):
        await provider.read(datetime(2026, 9, 27, 13, 0, tzinfo=TZ))


def test_version_is_single_sourced():
    """pyproject reads the version from __init__, so a bump cannot half-apply."""
    import tomllib
    from pathlib import Path

    from zerohero_dynamic_control import __version__

    pt = tomllib.loads(Path("pyproject.toml").read_text())
    assert "version" in pt["project"].get("dynamic", []), "version must be dynamic"
    assert pt["tool"]["setuptools"]["dynamic"]["version"]["attr"] == \
        "zerohero_dynamic_control.__version__"
    assert __version__.count(".") == 2


def test_compose_files_pass_every_secret_the_example_config_needs():
    """config.example.yaml references ${FOXESS_SERIAL}, so a compose file that
    only passes FOXESS_API_KEY produces a container that exits at startup. The
    two files are edited separately and drifted apart once already."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    # Only ACTIVE lines: the example also shows ${SOLCAST_API_KEY} and ${HA_TOKEN}
    # inside commented-out optional provider blocks, which nothing must supply.
    active = "\n".join(
        line for line in (root / "config.example.yaml").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    required = set(re.findall(r"\$\{([A-Z_]+)\}", active))
    assert "FOXESS_SERIAL" in required, "guard assumes the example uses ${FOXESS_SERIAL}"

    for name in ("docker-compose.yml", "docker-compose.registry.yml",
                 "docker-compose.synology.yml.example", ".env.example"):
        path = root / name
        if not path.exists():
            continue
        text = path.read_text()
        for var in required:
            assert var in text, f"{name} never supplies {var}, which config.example.yaml needs"


# ------------------------------------------- FoxESS rejects overlapping groups
ALL_DAY_DEFAULT = {"enable": 1, "startHour": 0, "startMinute": 0,
                   "endHour": 23, "endMinute": 59, "workMode": "SelfUse"}


@pytest.mark.asyncio
async def test_all_day_default_group_is_never_written_back():
    """The API reports a 00:00-23:59 SelfUse group that does NOT appear in the
    FoxESS app — an implicit default, not something the owner set. Writing it
    back alongside our window is rejected with errno 42023 'Time overlap', which
    is exactly what stopped the controller taking over on its first live evening.
    """
    from zerohero_dynamic_control.models import BatteryMode

    client = FakeFoxESS(responses={
        "/op/v1/device/scheduler/get": {"groups": [FREE_CHARGE_GROUP, ALL_DAY_DEFAULT]},
        "/op/v0/device/scheduler/get": {"groups": [FREE_CHARGE_GROUP, ALL_DAY_DEFAULT]},
    })
    ctl = build_controller(client)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 9, 27, 18, 0, tzinfo=TZ))

    groups = [c for c in client.calls if c[0].endswith("/enable")][-1][1]["groups"]
    assert ALL_DAY_DEFAULT not in groups, "the all-day default must not be written back"
    assert FREE_CHARGE_GROUP in groups, "the owner's real group must survive"
    assert any(g["workMode"] == "ForceDischarge" for g in groups)


@pytest.mark.asyncio
async def test_no_two_written_groups_share_a_minute():
    """FoxESS validates that groups never overlap, so whatever we send must be
    disjoint or the whole write is refused and the controller does nothing."""
    from zerohero_dynamic_control.models import BatteryMode

    client = FakeFoxESS(responses={
        "/op/v1/device/scheduler/get": {"groups": [FREE_CHARGE_GROUP, ALL_DAY_DEFAULT,
                                                   {"enable": 1, "startHour": 18, "startMinute": 0,
                                                    "endHour": 19, "endMinute": 5,
                                                    "workMode": "ForceDischarge"}]},
    })
    ctl = build_controller(client)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 9, 27, 18, 0, tzinfo=TZ))

    groups = [c for c in client.calls if c[0].endswith("/enable")][-1][1]["groups"]
    spans = sorted((g["startHour"] * 60 + g["startMinute"],
                    g["endHour"] * 60 + g["endMinute"]) for g in groups)
    for (s1, e1), (s2, _) in zip(spans, spans[1:], strict=False):
        assert e1 < s2, f"groups overlap: {s1//60}:{s1%60:02d}-{e1//60}:{e1%60:02d} vs {s2//60}:{s2%60:02d}"


@pytest.mark.asyncio
async def test_overlap_error_is_reported_with_the_groups_that_caused_it():
    from zerohero_dynamic_control.controllers.base import ControllerError
    from zerohero_dynamic_control.models import BatteryMode

    class Overlapping(FakeFoxESS):
        async def _send(self, path, body, method):
            self.calls.append((path, body))
            if path.endswith("/enable"):
                return {"errno": 42023, "msg": "Time overlap, please reselect time"}
            return {"errno": 0, "result": {}}

    ctl = build_controller(Overlapping())
    with pytest.raises(ControllerError, match="42023"):
        await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 9, 27, 18, 0, tzinfo=TZ))


# ---------------------------------------------- surviving a mid-window restart
@pytest.mark.asyncio
async def test_baseline_is_persisted_so_a_restart_cannot_lose_it(tmp_path):
    """Once we have written our group, the inverter no longer holds the owner's.
    A restart that re-read it would adopt OUR group as the baseline and restore
    that at 21:00, permanently destroying theirs."""
    from zerohero_dynamic_control.controllers.foxess import FoxESSController
    from zerohero_dynamic_control.models import BatteryMode

    owner = {"enable": 1, "startHour": 18, "startMinute": 0, "endHour": 19,
             "endMinute": 5, "workMode": "ForceDischarge", "fdPwr": 10000,
             "fdSoc": 10, "minSocOnGrid": 10, "maxSoc": 100}
    path = tmp_path / "baseline.json"
    client = FakeFoxESS(responses={"/op/v1/device/scheduler/get":
                                   {"groups": [FREE_CHARGE_GROUP, owner]}})
    cfg = AppConfig()
    ctl = FoxESSController(client, "SN", window_start=cfg.plan.credit_window_start,
                           window_end=cfg.plan.credit_window_end, min_soc_on_grid_pct=10,
                           max_power_kw=10.0, baseline_path=path)
    await ctl.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 9, 27, 18, 0, tzinfo=TZ))
    assert path.exists(), "baseline was never persisted"

    # Simulate a restart: the inverter now reports OUR group, not the owner's.
    ours = [c for c in client.calls if c[0].endswith("/enable")][-1][1]["groups"]
    client2 = FakeFoxESS(responses={"/op/v1/device/scheduler/get": {"groups": ours}})
    ctl2 = FoxESSController(client2, "SN", window_start=cfg.plan.credit_window_start,
                            window_end=cfg.plan.credit_window_end, min_soc_on_grid_pct=10,
                            max_power_kw=10.0, baseline_path=path)
    await ctl2.restore(now=datetime(2026, 9, 27, 21, 0, tzinfo=TZ), reason="close")
    restored = [c for c in client2.calls if c[0].endswith("/enable")][-1][1]["groups"]
    assert owner in restored, "the owner's original group was not recovered"
    assert not path.exists(), "the persisted copy should be cleared after a restore"


@pytest.mark.asyncio
async def test_our_own_group_is_never_adopted_as_the_baseline(tmp_path):
    """Belt and braces for the same failure when no persisted copy exists."""
    from zerohero_dynamic_control.controllers.foxess import FoxESSController
    from zerohero_dynamic_control.models import BatteryMode

    cfg = AppConfig()
    ctl = FoxESSController(FakeFoxESS(), "SN", window_start=cfg.plan.credit_window_start,
                           window_end=cfg.plan.credit_window_end, min_soc_on_grid_pct=10,
                           max_power_kw=10.0)
    ours = ctl._group("ForceDischarge", 2.2, 25)
    ours["endHour"] = ctl._end_hour()
    assert ctl._is_ours(ours)

    client = FakeFoxESS(responses={"/op/v1/device/scheduler/get":
                                   {"groups": [FREE_CHARGE_GROUP, ours]}})
    ctl2 = FoxESSController(client, "SN", window_start=cfg.plan.credit_window_start,
                            window_end=cfg.plan.credit_window_end, min_soc_on_grid_pct=10,
                            max_power_kw=10.0, baseline_path=tmp_path / "b.json")
    await ctl2.set_mode(BatteryMode.FORCE_EXPORT, now=datetime(2026, 9, 27, 18, 30, tzinfo=TZ))
    assert ctl2._baseline == [FREE_CHARGE_GROUP]
