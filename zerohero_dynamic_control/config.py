"""Typed configuration, loaded from ``config.yaml`` and validated with pydantic."""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .models import AllocationShape, ObjectiveMode
from .tariff import Tariff


def _from_env(value: Any, field: str) -> Any:
    """Expand a ``${VAR}`` config value from the environment.

    Fails loudly and by name when the variable is unset. Returning None instead
    would surface much later as a confusing "API key is required", or worse, as a
    FoxESS 40256 "illegal signature" that reads like a wrong key rather than a
    missing one.
    """
    if not (isinstance(value, str) and value.startswith("${") and value.endswith("}")):
        return value
    name = value[2:-1]
    resolved = os.environ.get(name)
    if resolved is None or not resolved.strip():
        raise ValueError(
            f"{field} is set to {value} but ${name} is not in the environment. "
            f"In Docker: put {name}=... in a .env file beside your compose file "
            f"(plain KEY=VALUE, no 'export'). In a shell: set -a; source .env; set +a"
        )
    return resolved.strip().strip('"').strip("'")


def _parse_time(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        parts = [int(p) for p in value.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return time(*parts[:3])
    raise TypeError(f"cannot parse time from {value!r}")


class SiteConfig(BaseModel):
    name: str = "the reference site Home"
    latitude: float = -33.7   # ~1 dp is ample: see config.yaml
    longitude: float = 151.0
    timezone: str = "Australia/Sydney"
    postcode: str = ""
    network: str = "Ausgrid"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class BatteryConfig(BaseModel):
    usable_capacity_kwh: float = Field(47.0, gt=0)
    max_discharge_kw: float = Field(10.0, gt=0)
    max_charge_kw: float = Field(10.0, gt=0)

    min_reserve_soc_pct: float = Field(25.0, ge=0, le=100)
    """Planning reserve: the engine will not *plan* to go below this."""

    emergency_floor_soc_pct: float = Field(10.0, ge=0, le=100)
    """Hard floor. The live loop may dip below the planning reserve down to here to
    protect the $1 credit, but never below."""

    allow_reserve_breach_for_credit: bool = True

    discharge_efficiency: float = Field(0.95, gt=0, le=1.0)
    """DC drawn from cells -> AC delivered at the meter."""
    charge_efficiency: float = Field(0.95, gt=0, le=1.0)

    ramp_kw_per_minute: float = Field(30.0, gt=0)
    """How fast the inverter can change setpoint. Used by the simulator and by the
    control loop to avoid asking for impossible step changes."""

    @model_validator(mode="after")
    def _check_floors(self) -> BatteryConfig:
        if self.emergency_floor_soc_pct > self.min_reserve_soc_pct:
            raise ValueError("emergency_floor_soc_pct must be <= min_reserve_soc_pct")
        return self


class InverterConfig(BaseModel):
    ac_limit_kw: float = Field(10.0, gt=0)
    """Continuous AC throughput of the hybrid inverter."""

    solar_shares_ac_limit: bool = True
    """True for a hybrid inverter (PV and battery share one AC port).
    Set False if PV is on a separate AC-coupled inverter."""

    grid_export_limit_kw: float = Field(10.0, gt=0)
    """Network/DNSP export limit. Ausgrid commonly allows 5 kW/phase."""


class PlanConfig(BaseModel):
    """GloBird ZEROHERO plan rules and prices.

    The windows here mirror the plan features printed on the bill:
        Peak 16:00-23:00 | Offpeak 11:00-14:00 | Shoulder 14:00-16:00 & 23:00-11:00
        Super Export top up 18:00-21:00 | ZeroHero 18:00-21:00
    """

    tariff: Tariff = Field(default_factory=Tariff)

    free_charge_start: time = time(11, 0)
    free_charge_end: time = time(14, 0)

    credit_window_start: time = time(18, 0)
    credit_window_end: time = time(21, 0)

    import_limit_kwh_per_hour: float = Field(0.03, gt=0)
    """Hard rule: import must stay below this in EVERY hour of the credit window."""

    joined_before_july_2026: bool = True
    """Free 11:00-14:00 charging applies to customers who joined before 1 Jul 2026."""

    _time_fields = ("free_charge_start", "free_charge_end", "credit_window_start", "credit_window_end")

    @field_validator(*_time_fields, mode="before")
    @classmethod
    def _coerce_times(cls, v: Any) -> time:
        return _parse_time(v)

    @model_validator(mode="after")
    def _sync_tariff_windows(self) -> PlanConfig:
        """Keep the tariff's copies of the windows in step with the plan config so
        there is exactly one place to change them."""
        self.tariff.zerohero_start = self.credit_window_start
        self.tariff.zerohero_end = self.credit_window_end
        self.tariff.zerohero_import_limit_kwh_per_hour = self.import_limit_kwh_per_hour
        self.tariff.free_charge_start = self.free_charge_start
        self.tariff.free_charge_end = self.free_charge_end
        return self

    # --- convenience passthroughs used across the codebase -------------------
    @property
    def daily_credit_aud(self) -> float:
        return self.tariff.zerohero_credit_aud

    @property
    def super_export_cap_kwh(self) -> float:
        return self.tariff.super_export_cap_kwh

    @property
    def in_window_export_rate(self) -> float:
        """$/kWh received for export inside 18:00-21:00 (FiT + Super Export top up)."""
        return self.tariff.export_rate(time(19, 0))


class FreeWindowAssuranceConfig(BaseModel):
    """Verification of the 11:00-14:00 free charging window.

    The window itself is owned by a ForceCharge group in the FoxESS app; this only
    checks that it is configured correctly and actually working. It is the most
    valuable three hours of the day (~30 kWh at $0.00 that would otherwise cost
    $0.407-$0.528), and it fails silently, so it is worth ~19 API calls a day to
    confirm rather than assume.
    """

    enabled: bool = True

    audit_lead_minutes: int = Field(10, ge=0, le=120)
    """Read the scheduler this many minutes before 11:00 to check the config."""

    check_interval_seconds: int = Field(600, ge=60, le=3600)
    """Behaviour polling cadence. 600 s = 18 calls across the window."""

    min_charge_power_kw: float = Field(0.5, ge=0)
    """Below this the battery is considered idle rather than charging."""

    startup_grace_minutes: int = Field(6, ge=0, le=60)
    """Ignore idle readings this soon after the window opens.

    Observed live: a group configured as 11:01-13:59 has not engaged at 11:00:01,
    and inverters take a moment to act on a schedule boundary regardless. Without
    this the very first check of every day reports a failure, and an alert that
    cries wolf daily is an alert nobody reads."""

    target_soc_pct: float = Field(100.0, ge=0, le=100)
    alert_soc_pct: float = Field(95.0, ge=0, le=100)
    """Flag the window as a problem if SOC at 14:00 is below this — but never
    above what the charge rate could physically reach from the starting SOC."""

    achievable_tolerance_pct: float = Field(3.0, ge=0, le=25)
    """Slack below the physically reachable SOC before calling it a failure.
    Covers taper near full, BMS balancing and load served during the window."""

    remediate: bool = False
    """If True, command ForceCharge ourselves when the battery is found idle.
    Default False: the FoxESS app owns this window, and two controllers writing
    the same scheduler is a good way to produce surprises. Turn it on once the
    audit has been clean for a while."""


class StrategyConfig(BaseModel):
    objective: ObjectiveMode = ObjectiveMode.ECONOMIC
    allocation: AllocationShape = AllocationShape.BLOCK

    min_force_export_kw: float = Field(3.0, ge=0)
    """Force-discharge only while the plan exports at least this much. Otherwise
    the inverter runs self-use for the window.

    Self-use is what protects the credit: the inverter matches the house load
    from its own meter within about a second. Force-discharge holds a fixed power
    and leaves anything above it to the grid, and the loop only sees the house
    through a cloud feed minutes old. So force-discharge is safe only when the
    export itself is big enough to absorb a load spike until the loop catches
    up — a kettle or oven is 2-3.6 kW. This is the owner's proven pattern:
    18:00-19:00 force-discharge at full power, then self-use, credit every day."""

    slot_minutes: int = Field(5, ge=1, le=30)
    """Planning resolution inside the credit window."""

    control_interval_seconds: int = Field(60, ge=10, le=300)
    """How often the live loop re-reads telemetry and re-issues a setpoint."""

    replan_minutes: int = Field(15, ge=1)
    """How often the live loop rebuilds the full plan from fresh forecasts."""

    decision_lead_minutes: int = Field(10, ge=0, le=120)
    """Run the decision engine this many minutes before the window opens."""

    import_safety_margin_kw: float = Field(0.25, ge=0)
    """Deliberate over-discharge so the meter always sees a trickle of export
    rather than hovering at exactly zero. This is the main insurance policy for
    the $1 credit — 0.25 kW costs ~0.75 kWh of battery across 3 hours."""

    energy_safety_buffer_kwh: float = Field(1.5, ge=0)
    """Extra battery energy held back beyond the forecast mandatory need, to absorb
    load forecast error (oven, aircon, EV) late in the window."""

    limit_to_super_export_cap: bool = True
    """Stop opportunistic export once the ~15 kWh Super Export cap is used, since
    beyond it the FiT drops back to the standard rate."""

    overnight_hours: float = Field(0.0, ge=0)
    """Hours from window close to the next free-charge window. 0 = compute it."""

    overnight_load_kw: float = Field(1.3, ge=0)
    """Average house load 21:00 -> 11:00, used to size the retained energy.
    Calibrated from the Jul-Aug bill: 42.09 kWh/day total, of which only 1.98 kWh/day
    came from paid (peak+shoulder) import, so the battery is already carrying most of
    the overnight load. Re-tune this from your own interval data."""

    morning_solar_to_battery_kwh: float = Field(6.0, ge=0)
    """Expected PV energy that reaches the battery between sunrise and 11:00. Raised
    in summer, near zero in mid-winter. Used by the ECONOMIC objective to work out how
    full the pack will already be when free charging starts."""

    weather_aware_overnight: bool = True
    """With objective=retain_overnight: size the night from the battery's learned
    drain and tomorrow morning's solar forecast (overnight.py), protecting the
    sunrise low point, instead of overnight_load_kw for every hour to 11:00."""

    overnight_learn_days: int = Field(7, ge=1, le=60)
    """Nights of history the drain rate is learned from (needs 3)."""

    forecast_check_models: list[str] = Field(
        default_factory=lambda: ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless"])
    """Other Open-Meteo weather models to predict the sunrise low with. When one of
    them puts the battery near the floor, the 17:50 message warns. Empty: off."""

    catch_up_on_start: bool = True
    """If the process starts while the credit window is already open, run it for
    the remaining time instead of waiting for tomorrow.

    Without this a single failure at 17:50 — a bad API call, a container
    restart, a NAS reboot — silently forfeits the whole evening, because the
    cron job does not fire again until the next day. That happened on the first
    live run: the job raised, and the controller then sat idle through a window
    it could still have driven."""

    abandon_credit_if_unwinnable: bool = True
    """If the pack cannot cover the whole window, the $1 credit is lost no matter what
    (the rule requires every hour to comply). In that case burning the battery down to
    the floor by 20:00 just means importing at the $0.528 peak rate afterwards, so the
    better move is to stop force-exporting and run ordinary self-consumption. Set False
    to keep trying anyway — worth it if your load forecast runs pessimistic."""

    unwinnable_margin_kwh: float = Field(0.5, ge=0)
    """Treat the window as winnable if the shortfall is under this — forecast noise."""

    revert_mode_on_target_reached: bool = False
    """If True, drop back to self-consumption as soon as the export target is hit.
    Default False: stay in controlled mode so we keep guaranteeing zero import."""

    charge_in_free_window: bool = False
    """Whether WE drive the free window. Default False: this site already has a
    ForceCharge group configured in the FoxESS app, so we verify it rather than
    fight it. See free_window_assurance."""

    free_window_target_soc_pct: float = Field(100.0, ge=0, le=100)

    free_window_assurance: FreeWindowAssuranceConfig = Field(
        default_factory=FreeWindowAssuranceConfig
    )

    fallback_discharge_kw: float = Field(3.0, ge=0)
    """Blind 'just force export at X kW until 21:00' power used when telemetry dies."""


class ForecastConfig(BaseModel):
    provider: Literal["static", "open_meteo", "solcast", "simulated"] = "static"
    pv_rating_kw: float = Field(10.0, gt=0)
    array_tilt_deg: float = Field(20.0, ge=0, le=90)
    """Roof pitch. 20 degrees suits a typical Australian tiled roof."""

    array_azimuth_deg: float | None = None
    """Direction the panels face, in Open-Meteo's convention: 0 = SOUTH,
    -90 = east, 90 = west, ±180 = north. There is NO hemisphere adjustment —
    0 means south in Sydney just as it does in London.

    Leave null to derive it from latitude: 180 (north-facing) below the equator,
    0 (south-facing) above. Set explicitly for an east/west split array."""

    solcast_api_key: str | None = None
    solcast_resource_id: str | None = None
    timeout_seconds: float = 10.0

    static_residual_solar_kwh: float = 1.0
    """Fallback: expected PV energy between 18:00 and sunset."""
    static_evening_load_kw: float = 1.6
    """Fallback: average house load across 18:00-21:00."""
    load_profile_kw: dict[str, float] = Field(default_factory=dict)
    """Optional hour-of-day -> kW map used by the static load forecaster."""
    learn_load_days: int = Field(7, ge=0)
    """Forecast the evening load from the mean of this many recent evenings of the
    controller's own readings (samples.jsonl). 0 turns it off. Slots without
    enough history fall back to load_profile_kw / static_evening_load_kw."""
    learn_load_min_days: int = Field(3, ge=1)
    """Evenings of history a 15-minute slot needs before its learned value is used."""

    @field_validator("solcast_api_key", mode="before")
    @classmethod
    def _expand_env(cls, v: Any) -> Any:
        return _from_env(v, "forecast.solcast_api_key")


class FoxESSModbusConfig(BaseModel):
    """Local Modbus TCP telemetry, read straight from the inverter.

    The cloud feed is a five-minute snapshot; this is seconds old, which is what
    it takes to see a kettle before it spends the hour's 30 Wh. When enabled it
    becomes the primary telemetry source and the cloud becomes the fallback.

    Run `zerohero modbus-probe` first: it reads both sources side by side.
    """

    enabled: bool = False
    host: str | None = None
    """IP of the inverter's built-in WL-H3-G2 logger, or of an RS485-to-TCP adapter
    wired to its COM port. Give it a DHCP reservation so it cannot move."""
    port: int = Field(502, gt=0, lt=65536)
    unit_id: int = Field(247, ge=0, le=255)
    """247 for FoxESS, over both the built-in logger and RS485."""
    timeout_seconds: float = Field(3.0, gt=0)
    cloud_fallback_interval_seconds: float = Field(60.0, ge=10)
    """While Modbus is down, read the cloud at most this often. Each read is one
    of the 1440 daily calls."""

    @model_validator(mode="after")
    def _host_when_enabled(self) -> FoxESSModbusConfig:
        if self.enabled and not self.host:
            raise ValueError("providers.foxess.modbus.enabled needs providers.foxess.modbus.host")
        return self


class FoxESSConfig(BaseModel):
    """FoxESS Cloud OpenAPI settings.

    Get the key from the FoxESS Cloud portal: User Profile -> API Management.
    Leave serial_number empty to auto-discover the first battery inverter.
    """

    api_key: str | None = None
    serial_number: str | None = None
    base_url: str = "https://www.foxesscloud.com"

    daily_call_limit: int = Field(1440, gt=0)
    """FoxESS allows 1440 interface calls per day per inverter. The client tracks
    spend against this and refuses non-critical calls near the limit."""
    call_reserve: int = Field(120, ge=0)
    """Calls held back so close-out and baseline restore can always run."""

    preserve_existing_schedule: bool = True
    """Read the owner's existing scheduler groups at startup and restore them at
    close-out. scheduler/enable replaces the whole list, so without this we would
    silently wipe whatever was configured in the FoxESS app."""

    invert_battery_power_sign: bool = False
    """Only used when the firmware exposes the signed invBatPower but not the
    unambiguous batChargePower/batDischargePower pair. Flip this if the simulator
    and the app disagree about whether the battery is charging."""

    modbus: FoxESSModbusConfig = Field(default_factory=FoxESSModbusConfig)

    @field_validator("api_key", "serial_number", mode="before")
    @classmethod
    def _expand_env(cls, v: Any) -> Any:
        return _from_env(v, "providers.foxess")


    @model_validator(mode="after")
    def _reject_unexpanded_placeholders(self) -> FoxESSConfig:
        """Catch a ${VAR} that no validator expanded.

        This is a version-skew guard. config.yaml is bind-mounted from the host
        while the code lives in the image, so a `git pull` can hand new config to
        old code. That happened: config.yaml gained `serial_number: ${FOXESS_SERIAL}`
        before the image had the validator to expand it, so the literal string was
        sent as the serial. FoxESS answered errno 0 with an empty result, which
        surfaced four layers away as "telemetry unavailable" and cost an hour.

        Failing here names the problem instead.
        """
        for field in ("api_key", "serial_number"):
            value = getattr(self, field, None)
            if isinstance(value, str) and "${" in value:
                raise ValueError(
                    f"providers.foxess.{field} is still the literal {value!r}. "
                    f"This code version cannot expand it — your config.yaml is newer "
                    f"than the running image. Rebuild the image, or put the literal "
                    f"value in config.yaml."
                )
        return self


class ProvidersConfig(BaseModel):
    battery: Literal["simulated", "foxess", "http", "homeassistant"] = "simulated"
    solar: Literal["simulated", "foxess", "http", "homeassistant"] = "simulated"
    load: Literal["simulated", "foxess", "http", "homeassistant"] = "simulated"
    foxess: FoxESSConfig = Field(default_factory=FoxESSConfig)
    base_url: str | None = None
    token: str | None = None
    entity_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("token", mode="before")
    @classmethod
    def _expand_env(cls, v: Any) -> Any:
        return _from_env(v, "providers.token")


class ControllerConfig(BaseModel):
    type: Literal["printing", "simulated", "foxess", "tesla_fleet", "homeassistant", "modbus"] = "printing"
    options: dict[str, Any] = Field(default_factory=dict)
    command_deadband_kw: float = Field(0.15, ge=0)
    """Suppress re-issuing a setpoint that differs by less than this. Saves API calls."""
    min_lower_interval_seconds: float = Field(0.0, ge=0)
    """Hold a LOWER setpoint back until this long after the last write.

    Raises always go out at once: they answer import, and import spends the
    hour's 30 Wh. Lowering only trims a margin of export, so it can wait. With
    fast local telemetry the loop sees every flicker of the load, and without
    this each one would cost a cloud write; 30 s keeps a three-hour window
    within a few hundred writes."""


class GloBirdConfig(BaseModel):
    """Fetch GloBird's own daily costs from the customer portal and record them.

    Credentials come from the environment only (GLOBIRD_EMAIL, GLOBIRD_PASSWORD
    in the .env beside the compose file), never from this file. Read-only.
    """

    enabled: bool = False
    fetch_times: list[str] = Field(default_factory=lambda: ["07:30", "10:30", "13:30", "16:30"])
    """Local times to look for newly published days. The portal publishes a day's
    costs some time the next day; each fetch records only what is new or revised."""
    days: int = Field(7, ge=1, le=60)
    """How far back each fetch looks, so a late revision is still picked up."""

    @field_validator("fetch_times")
    @classmethod
    def _valid_times(cls, v: list[str]) -> list[str]:
        for t in v:
            hh, _, mm = t.partition(":")
            if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError(f"globird.fetch_times: {t!r} is not HH:MM")
        return v


class NotifyConfig(BaseModel):
    """Daily ntfy push messages. The topic comes from NTFY_TOPIC in .env: anyone who
    knows it can read the messages, so it is a secret, not a setting."""

    enabled: bool = False
    server: str = "https://ntfy.sh"
    morning_deadline: str = "11:00"
    """Send yesterday's summary by this time even if GloBird has not published the
    day yet (bill marked pending; a follow-up carries it when it lands)."""
    evening: bool = True
    """Also send tonight's plan right after the 17:50 decision."""

    @field_validator("morning_deadline")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        hh, _, mm = v.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError(f"notify.morning_deadline: {v!r} is not HH:MM")
        return v


class LoggingConfig(BaseModel):
    level: str = "INFO"
    ledger_path: Path = Path("var/ledger.jsonl")
    decision_log_path: Path = Path("var/decisions.jsonl")
    samples_path: Path | None = Path("var/samples.jsonl")
    rich_tracebacks: bool = True


class ApiConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787


class SimulationConfig(BaseModel):
    enabled: bool = False
    scenario: str = "summer"
    speed: float = Field(0.0, ge=0)
    """0 = run as fast as possible. >0 = seconds of wall clock per simulated minute."""
    start_soc_pct: float = 85.0
    replay_csv: Path | None = None


class AppConfig(BaseModel):
    site: SiteConfig = Field(default_factory=SiteConfig)
    battery: BatteryConfig = Field(default_factory=BatteryConfig)
    inverter: InverterConfig = Field(default_factory=InverterConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    controller: ControllerConfig = Field(default_factory=ControllerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    globird: GloBirdConfig = Field(default_factory=GloBirdConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        if self.battery.max_discharge_kw > self.inverter.ac_limit_kw:
            # Not fatal — the engine clamps anyway — but worth surfacing early.
            self.battery.max_discharge_kw = self.inverter.ac_limit_kw
        return self

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        if path is None:
            for candidate in (Path("config.yaml"), Path("config.yml")):
                if candidate.exists():
                    path = candidate
                    break
        if path is None:
            example = Path("config.example.yaml")
            if example.exists():
                raise FileNotFoundError(
                    "no config.yaml found. Copy the template and edit it:\n"
                    "    cp config.example.yaml config.yaml\n"
                    "Running on built-in defaults would silently use another site's "
                    "coordinates, tariff and battery limits."
                )
            return cls()
        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(raw)
