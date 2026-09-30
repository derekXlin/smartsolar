"""Domain models shared across providers, the decision engine and controllers.

All power values are in kW and use a single, consistent sign convention:

    solar_kw      >= 0      PV AC output
    load_kw       >= 0      house consumption
    battery_kw    > 0       discharging (battery -> AC bus)
                  < 0       charging   (AC bus -> battery)
    grid_kw       > 0       IMPORTING from the grid   (what kills the $1 credit)
                  < 0       EXPORTING to the grid     (what earns Super Export)

The site power balance that everything below relies on is:

    solar_kw + battery_kw = load_kw + export_kw            (export_kw = -grid_kw)
    <=>  grid_kw = load_kw - solar_kw - battery_kw
"""

from __future__ import annotations

import math
from datetime import datetime, time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class BatteryMode(StrEnum):
    """Vendor-neutral battery operating modes.

    Concrete controllers map these onto whatever the hardware calls them, e.g.
    Tesla "Time-Based Control / autonomous", Sungrow "Forced mode", Sigenergy
    "TOU", GoodWe "Eco mode", or a Modbus register write.
    """

    SELF_CONSUMPTION = "self_consumption"
    """Normal behaviour: PV -> load -> battery -> grid."""

    FORCE_EXPORT = "force_export"
    """Discharge at a commanded power regardless of house load (our 18:00 mode)."""

    FORCE_CHARGE = "force_charge"
    """Charge at a commanded power (used in the 11:00-14:00 free window)."""

    HOLD = "hold"
    """Idle the battery: no charge, no discharge."""

    BACKUP = "backup"
    """Reserve-only / storm-watch style mode."""


class ObjectiveMode(StrEnum):
    """How aggressively to trade stored energy for Super Export revenue."""

    ECONOMIC = "economic"
    """Default. Compare the marginal value of a stored kWh (the import it displaces,
    $0.407-$0.528) against its export value inside the window ($0.10), and sell only
    the energy that tomorrow's free 11:00-14:00 charge would otherwise strand."""

    GUARANTEE_CREDIT_ONLY = "guarantee_credit_only"
    """Discharge only enough to hold grid import at zero. Maximises SOC at 21:00."""

    RETAIN_OVERNIGHT = "retain_overnight"
    """Keep enough to run the house until the next free-charge window, export the rest."""

    MAXIMISE_EXPORT = "maximise_export"
    """Export everything above the hard minimum reserve, up to the Super Export cap."""


class AllocationShape(StrEnum):
    """Shape of the opportunistic export power profile across the window."""

    CONSTANT = "constant"
    """Flat export power for the whole window (simplest, gentlest on the inverter)."""

    FRONT_LOADED = "front_loaded"
    """Export harder early. Banks Super Export kWh before anything can go wrong."""

    SOLAR_FOLLOWING = "solar_following"
    """Export more while PV is still producing, so battery drain stays level."""

    BLOCK = "block"
    """Export at full power from the window's start until the budget is spent, then
    stop. The only shape that works with self-use between exports: a thin export
    spread across the window is too small to act as a buffer against load spikes,
    so it would be force-discharged with no protection or not sold at all."""


class Telemetry(BaseModel):
    """A single instantaneous read of the site."""

    timestamp: datetime
    soc_pct: float = Field(ge=0.0, le=100.0)
    battery_energy_kwh: float = Field(ge=0.0)
    solar_kw: float = 0.0
    load_kw: float = 0.0
    battery_kw: float = 0.0
    grid_kw: float = 0.0
    stale: bool = False
    """True when this sample came from a fallback/cached path rather than live hardware."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def export_kw(self) -> float:
        return max(0.0, -self.grid_kw)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def import_kw(self) -> float:
        return max(0.0, self.grid_kw)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_load_kw(self) -> float:
        """Load the battery must cover to hold grid import at zero."""
        return self.load_kw - self.solar_kw


class ForecastPoint(BaseModel):
    """One point on a forecast curve."""

    timestamp: datetime
    solar_kw: float = 0.0
    load_kw: float = 0.0


class SlotPlan(BaseModel):
    """Planned behaviour for one time slot inside the credit window."""

    start: datetime
    end: datetime
    solar_kw: float
    load_kw: float
    mandatory_discharge_kw: float
    """Battery AC power needed purely to hold grid import at ~zero."""
    export_discharge_kw: float
    """Extra battery AC power dedicated to Super Export revenue."""
    export_headroom_kw: float
    """Maximum additional export this slot can physically accept."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def battery_ac_kw(self) -> float:
        return self.mandatory_discharge_kw + self.export_discharge_kw

    @computed_field  # type: ignore[prop-decorator]
    @property
    def grid_export_kw(self) -> float:
        """Total site export: battery surplus plus any PV the house cannot absorb."""
        return max(0.0, self.solar_kw + self.battery_ac_kw - self.load_kw)


class RiskLevel(StrEnum):
    OK = "ok"
    WATCH = "watch"
    AT_RISK = "at_risk"


class Decision(BaseModel):
    """The output of the decision engine — everything the control loop needs."""

    made_at: datetime
    window_start: datetime
    window_end: datetime

    starting_soc_pct: float
    starting_energy_kwh: float

    target_export_kwh: float
    """Total energy we intend to push to the grid during the window."""
    mandatory_discharge_kwh: float
    """AC energy the battery must supply just to hold import at zero."""
    opportunistic_export_kwh: float
    """AC energy dedicated to Super Export revenue on top of the mandatory amount."""
    passive_solar_export_kwh: float
    """Baseline export that occurs even with zero opportunistic discharge: PV surplus
    early in the window, plus the deliberate import-safety margin."""

    recommended_discharge_kw: float
    """Average battery AC discharge power across the window."""
    peak_discharge_kw: float

    expected_final_soc: float
    expected_final_energy_kwh: float

    battery_dc_drawn_kwh: float
    reserve_floor_soc_pct: float
    overnight_retention_kwh: float

    slots: list[SlotPlan] = Field(default_factory=list)
    credit_achievable: bool = True
    """False when the pack physically cannot hold import at zero for the whole window."""
    recommended_mode: BatteryMode = BatteryMode.FORCE_EXPORT
    """Mode the controller should enter at window open."""
    energy_shortfall_kwh: float = 0.0
    """How much more stored energy would have been needed to guarantee the credit."""
    expected_net_aud: float = 0.0
    """Projected net cost for the day. Negative means the day earns money."""
    pnl_summary: str = ""
    risk: RiskLevel = RiskLevel.OK
    degraded: bool = False
    """True when the plan was built on fallback data (forecast or telemetry failure)."""
    telemetry_assumed: bool = False
    """True when SOC could NOT be read and a stand-in value was used. Every number
    in this decision is then a guess about the battery, not a measurement."""
    rationale: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"target_export_kwh={self.target_export_kwh:.2f} "
            f"recommended_discharge_kw={self.recommended_discharge_kw:.2f} "
            f"expected_final_soc={self.expected_final_soc:.1f}%"
        )


class ControlCommand(BaseModel):
    """A single instruction issued to the battery controller."""

    timestamp: datetime
    mode: BatteryMode
    power_kw: float = 0.0
    """Requested battery AC power, signed: >0 discharge, <0 charge."""
    reason: str = ""


BREACH_CERTAINTY_FACTOR = 3.0
"""How many times the hourly limit an ESTIMATE must reach before the ledger calls
the credit lost. Between 1x and this, the bill decides. Set from two evenings of
cloud telemetry (see HourImport.clearly_breached); revisit with more bills."""


class HourImport(BaseModel):
    """Import energy accumulated inside one clock hour of the credit window."""

    hour_start: datetime
    imported_kwh: float = 0.0
    limit_kwh: float = 0.03
    observed_minutes: float = 0.0
    """How much of this hour telemetry actually covered. Zero import over an hour
    nobody watched is not evidence of anything."""
    span_minutes: float = 60.0
    """The part of this clock hour that falls inside the credit window."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def headroom_kwh(self) -> float:
        return self.limit_kwh - self.imported_kwh

    @computed_field  # type: ignore[prop-decorator]
    @property
    def breached(self) -> bool:
        """The ESTIMATE is at or over the limit. Enough to make the loop cautious,
        not enough to call the credit lost — see clearly_breached."""
        # Strict '<' in the plan rules; use a tiny epsilon so float noise at exactly
        # the limit is treated as a breach rather than a pass.
        return self.imported_kwh >= self.limit_kwh - 1e-9

    @computed_field  # type: ignore[prop-decorator]
    @property
    def clearly_breached(self) -> bool:
        """Far enough over that estimation error cannot explain it.

        The estimate integrates readings the cloud refreshes every ~5 minutes, so
        a momentary draw the inverter corrects in seconds is counted as five
        minutes of import. On 28 Sep the 19:00 hour estimated 61 Wh (2x the limit)
        and GloBird paid the credit; on 27 Sep the 18:00 hour estimated 165 Wh
        (5.5x) and the credit was lost. The line sits between those two.
        """
        return self.imported_kwh >= BREACH_CERTAINTY_FACTOR * self.limit_kwh

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verified(self) -> bool:
        """Watched closely enough that a clean result means something.

        90% leaves room for a restart or a dropped poll, but not for the 17
        unwatched minutes that preceded takeover on the first live evening.
        """
        return self.observed_minutes >= 0.9 * self.span_minutes


class DailyOutcome(BaseModel):
    """One row of the ledger — written after every credit window closes."""

    date: str
    decision: Decision | None = None
    exported_kwh: float = 0.0
    imported_kwh: float = 0.0
    hourly_import: list[HourImport] = Field(default_factory=list)
    final_soc_pct: float = 0.0
    final_energy_kwh: float = 0.0
    credit_secured: bool = False
    """True only when every hour was both under the limit AND watched."""
    credit_verified: bool = False
    """Every hour of the window was covered by telemetry. False with no breach
    means the result is unknown, not that the credit was missed."""
    partial: bool = False
    """Closed out before the window ended (shutdown, crash). A later row for the
    same date supersedes it."""
    super_export_kwh: float = 0.0
    estimated_revenue_aud: float = 0.0
    notes: list[str] = Field(default_factory=list)


class FreeWindowOutcome(BaseModel):
    """Verdict on one 11:00-14:00 free charging window.

    Split into a configuration check and a behaviour check because they fail
    independently: the group can be present but the battery already full, or the
    battery can be charging from PV while the ForceCharge group is quietly disabled.
    """

    date: str
    ok: bool = False
    schedule_ok: bool = True
    """An enabled ForceCharge group actually covers the window."""

    samples: int = 0
    charging_samples: int = 0
    failed_samples: int = 0

    start_soc_pct: float = 0.0
    final_soc_pct: float = 0.0
    target_soc_pct: float = 100.0
    achievable_soc_pct: float = 100.0
    """Highest SOC the charge rate could physically reach from this start SOC."""
    energy_added_kwh: float = 0.0

    unclaimed_free_kwh: float = 0.0
    """Capacity left unfilled — energy that must now be bought at a paid rate."""
    missed_value_aud: float = 0.0

    remediated: bool = False
    findings: list[str] = Field(default_factory=list)


def soc_to_energy(soc_pct: float, usable_capacity_kwh: float) -> float:
    return max(0.0, soc_pct) / 100.0 * usable_capacity_kwh


def energy_to_soc(energy_kwh: float, usable_capacity_kwh: float) -> float:
    if usable_capacity_kwh <= 0:
        return 0.0
    return max(0.0, min(100.0, energy_kwh / usable_capacity_kwh * 100.0))


def combine_date_time(day: datetime, clock_time: time) -> datetime:
    """Attach a wall-clock time to the date of ``day``, preserving tzinfo."""
    return day.replace(
        hour=clock_time.hour,
        minute=clock_time.minute,
        second=clock_time.second,
        microsecond=0,
    )


def isclose(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, abs_tol=tol)
