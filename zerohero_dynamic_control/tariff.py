"""GloBird ZEROHERO tariff model (NSW / Ausgrid).

Rates below are taken verbatim from a real GloBird ZEROHERO tax invoice for a
NSW/Ausgrid site, billing period 15-Jul-2026 to 11-Aug-2026. They are
GST-inclusive, which is what the bill quotes, so every dollar figure this module
produces is GST-inclusive.

CHECK THESE AGAINST YOUR OWN BILL before trusting any number this produces.
Plans change and rates differ by state and network.

Why this module exists
----------------------
The whole control strategy is an arbitrage, and it only works if the engine knows
the actual prices:

    import 11:00-14:00      $0.00000/kWh    <- free
    import 14:00-16:00      $0.40700/kWh
    import 16:00-23:00      $0.52800/kWh    <- the credit window sits inside peak
    import 23:00-11:00      $0.40700/kWh

    export 16:00-23:00     -$0.02000/kWh
    export 23:00-16:00      $0.00000/kWh    <- daytime export earns nothing at all
    export 18:00-21:00     -$0.08000/kWh    extra "Super Export top up", first ~15 kWh

So a kWh pushed out during 18:00-21:00 earns $0.10, and a kWh pulled in during
11:00-14:00 costs nothing. That is the trade the controller exists to exploit.

The counter-pressure is that a kWh *kept* in the battery is worth whatever import
it displaces later - up to $0.528 in the evening peak, $0.407 overnight. Keeping is
worth 4-5x more than selling, but only while that energy genuinely displaces import.
Once the house's overnight need is covered and tomorrow's free window can refill the
battery anyway, the surplus is stranded and is worth exactly its export value.
`marginal_value_of_stored_energy` below is where that comparison is made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, Field


def _in_window(t: time, start: time, end: time) -> bool:
    """Membership test for a [start, end) window that may wrap past midnight."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


class TouPeriod(BaseModel):
    name: str
    start: time
    end: time
    rate_aud_per_kwh: float

    def covers(self, t: time) -> bool:
        return _in_window(t, self.start, self.end)


class Tariff(BaseModel):
    """Import and export prices by time of day, plus the ZeroHero credit rules."""

    daily_supply_charge_aud: float = 1.584

    import_periods: list[TouPeriod] = Field(
        default_factory=lambda: [
            TouPeriod(name="offpeak", start=time(11, 0), end=time(14, 0), rate_aud_per_kwh=0.0),
            TouPeriod(name="shoulder_pm", start=time(14, 0), end=time(16, 0), rate_aud_per_kwh=0.407),
            TouPeriod(name="peak", start=time(16, 0), end=time(23, 0), rate_aud_per_kwh=0.528),
            TouPeriod(name="shoulder_night", start=time(23, 0), end=time(11, 0), rate_aud_per_kwh=0.407),
        ]
    )

    export_periods: list[TouPeriod] = Field(
        default_factory=lambda: [
            # Bill line "Solar/Generation Feed in (4pm-11pm)".
            TouPeriod(name="fit_evening", start=time(16, 0), end=time(23, 0), rate_aud_per_kwh=0.02),
            # Bill line "Solar/Generation Feed in (11pm-4pm)" - pays nothing.
            TouPeriod(name="fit_day", start=time(23, 0), end=time(16, 0), rate_aud_per_kwh=0.0),
        ]
    )

    super_export_start: time = time(18, 0)
    super_export_end: time = time(21, 0)
    super_export_cap_kwh: float = Field(15.0, gt=0)
    super_export_topup_aud_per_kwh: float = 0.08
    """Bill line "Super Export top up - Step 1". Stacks ON TOP of the evening FiT."""

    zerohero_credit_aud: float = 1.0
    zerohero_start: time = time(18, 0)
    zerohero_end: time = time(21, 0)
    zerohero_import_limit_kwh_per_hour: float = Field(0.03, gt=0)

    free_charge_start: time = time(11, 0)
    free_charge_end: time = time(14, 0)

    def import_rate(self, t: time) -> float:
        for p in self.import_periods:
            if p.covers(t):
                return p.rate_aud_per_kwh
        return 0.0

    def export_rate(self, t: time, *, within_super_export_cap: bool = True) -> float:
        """Total received per exported kWh at time ``t``."""
        base = 0.0
        for p in self.export_periods:
            if p.covers(t):
                base = p.rate_aud_per_kwh
                break
        if within_super_export_cap and _in_window(t, self.super_export_start, self.super_export_end):
            base += self.super_export_topup_aud_per_kwh
        return base

    def blended_import_rate(self, start: time, hours: float, step_minutes: int = 15) -> float:
        """Average import price over ``hours`` starting at ``start``.

        Used to value retained energy: between 21:00 and 11:00 the house crosses the
        $0.528 peak (21:00-23:00) and the $0.407 shoulder (23:00-11:00), so neither
        single rate is the right comparison against the $0.10 export price.
        """
        if hours <= 0:
            return self.import_rate(start)
        steps = max(1, int(round(hours * 60 / step_minutes)))
        base = datetime.combine(date(2000, 1, 1), start)
        total = 0.0
        for i in range(steps):
            moment = (base + timedelta(minutes=step_minutes * (i + 0.5))).time()
            total += self.import_rate(moment)
        return total / steps

    def in_credit_window(self, t: time) -> bool:
        return _in_window(t, self.zerohero_start, self.zerohero_end)

    def in_free_charge_window(self, t: time) -> bool:
        return _in_window(t, self.free_charge_start, self.free_charge_end)


@dataclass
class StoredEnergyValuation:
    """Result of asking 'what is the next kWh in the battery actually worth?'"""

    displaceable_kwh: float
    """Energy that will genuinely displace paid import before the next free window."""
    displacement_value_aud_per_kwh: float
    """Blended import price that displaced energy avoids."""
    stranded_kwh: float
    """Energy that cannot be used before tomorrow's free window refills the battery,
    and is therefore worth only its export price."""
    export_value_aud_per_kwh: float
    reasons: list[str] = field(default_factory=list)

    @property
    def should_export_stranded(self) -> bool:
        return self.export_value_aud_per_kwh > 0 and self.stranded_kwh > 0


def marginal_value_of_stored_energy(
    *,
    energy_at_window_close_kwh: float,
    usable_capacity_kwh: float,
    overnight_consumption_kwh: float,
    morning_solar_to_battery_kwh: float,
    free_window_recharge_kwh: float,
    reserve_floor_kwh: float,
    blended_import_rate_aud_per_kwh: float,
    export_rate_aud_per_kwh: float,
) -> StoredEnergyValuation:
    """Split the energy sitting in the battery at 21:00 into 'useful' and 'stranded'.

    The logic, in words:

    Between the close of the credit window and the start of tomorrow's free charging
    window the house draws ``overnight_consumption_kwh`` and the morning sun puts back
    ``morning_solar_to_battery_kwh``. So the battery arrives at 11:00 tomorrow holding

        E_1100 = E_2100 - overnight_consumption + morning_solar_to_battery

    During the free window we can push in ``free_window_recharge_kwh`` at zero cost
    (3 hours x the charge power limit, plus whatever the array contributes), but only
    up to the physical capacity of the pack. Anything that would push us past capacity
    is capacity we could not use - which means the energy occupying that space tonight
    was never going to displace a paid import. It is stranded, and stranded energy is
    worth exactly its export price, which is why selling it at $0.10 beats hoarding it.

    Conversely the energy that *does* get consumed overnight displaces import at the
    shoulder/peak rate, which is several times the export rate. That portion should
    never be sold.
    """
    reasons: list[str] = []

    e_1100 = energy_at_window_close_kwh - overnight_consumption_kwh + morning_solar_to_battery_kwh
    e_1100 = max(reserve_floor_kwh, e_1100)

    # Room left in the pack when free charging starts, and how much of the free
    # charge opportunity we can actually absorb.
    headroom_at_1100 = max(0.0, usable_capacity_kwh - e_1100)
    unusable_free_energy = max(0.0, free_window_recharge_kwh - headroom_at_1100)

    # Every kWh of free charging we cannot absorb is a kWh we should have sold.
    stranded = min(
        max(0.0, energy_at_window_close_kwh - reserve_floor_kwh),
        unusable_free_energy,
    )
    if unusable_free_energy > 0:
        reasons.append(
            f"tomorrow's free window offers {free_window_recharge_kwh:.1f} kWh but the pack "
            f"will only have {headroom_at_1100:.1f} kWh of headroom at 11:00 — "
            f"{unusable_free_energy:.1f} kWh of free energy would be wasted"
        )

    displaceable = max(0.0, energy_at_window_close_kwh - reserve_floor_kwh - stranded)
    if displaceable > 0:
        reasons.append(
            f"{displaceable:.1f} kWh will displace import at ~${blended_import_rate_aud_per_kwh:.3f}/kWh "
            f"(vs ${export_rate_aud_per_kwh:.3f}/kWh if exported) — worth keeping"
        )

    return StoredEnergyValuation(
        displaceable_kwh=displaceable,
        displacement_value_aud_per_kwh=blended_import_rate_aud_per_kwh,
        stranded_kwh=stranded,
        export_value_aud_per_kwh=export_rate_aud_per_kwh,
        reasons=reasons,
    )
