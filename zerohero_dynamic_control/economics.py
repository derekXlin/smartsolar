"""Dollar accounting: turn a plan (or a day's actuals) into a net daily P&L.

The user's goal is a zero-or-negative daily bill, so every decision is ultimately
judged here rather than in kWh. The daily ledger for this site is:

    cost  = daily supply charge                              $1.584
          + paid import (peak $0.528 / shoulder $0.407)
    credit= ZeroHero daily credit                            $1.000  (if compliant)
          + FiT on all export inside 16:00-23:00             $0.020/kWh
          + Super Export top up on the first ~15 kWh in-window $0.080/kWh

The headline: $1.584 of supply charge is fully covered by the $1 credit plus a
full 15 kWh Super Export run ($1.20 top-up + $0.30 FiT = $1.50), so a compliant,
fully-exporting day is cash-positive before usage charges. Everything the
controller does is an attempt to reach that state without incurring paid import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .tariff import Tariff


@dataclass
class DailyPnL:
    supply_charge_aud: float = 0.0
    import_cost_aud: float = 0.0
    zerohero_credit_aud: float = 0.0
    fit_revenue_aud: float = 0.0
    super_export_topup_aud: float = 0.0
    breakdown: list[str] = field(default_factory=list)

    @property
    def total_credits_aud(self) -> float:
        return self.zerohero_credit_aud + self.fit_revenue_aud + self.super_export_topup_aud

    @property
    def total_costs_aud(self) -> float:
        return self.supply_charge_aud + self.import_cost_aud

    @property
    def net_aud(self) -> float:
        """Positive = the day cost money. Negative = the day earned money."""
        return self.total_costs_aud - self.total_credits_aud

    @property
    def is_cash_positive(self) -> bool:
        return self.net_aud <= 0.0

    def format(self) -> str:
        sign = "EARNED" if self.net_aud < 0 else "COST"
        return (
            f"supply ${self.supply_charge_aud:.2f} + import ${self.import_cost_aud:.2f} "
            f"- credit ${self.zerohero_credit_aud:.2f} - FiT ${self.fit_revenue_aud:.2f} "
            f"- topup ${self.super_export_topup_aud:.2f} => {sign} ${abs(self.net_aud):.2f}"
        )


def value_export(
    tariff: Tariff,
    *,
    kwh: float,
    at: time,
    super_export_used_kwh: float = 0.0,
) -> tuple[float, float]:
    """Value ``kwh`` exported at ``at``. Returns (fit_aud, topup_aud).

    The Super Export top-up only applies to the portion still under the daily cap,
    so a partially-used cap splits the energy across two prices.
    """
    fit = 0.0
    for p in tariff.export_periods:
        if p.covers(at):
            fit = p.rate_aud_per_kwh
            break
    fit_aud = kwh * fit

    topup_aud = 0.0
    if tariff.zerohero_start <= at < tariff.zerohero_end or (
        tariff.super_export_start <= at < tariff.super_export_end
    ):
        eligible = max(0.0, min(kwh, tariff.super_export_cap_kwh - super_export_used_kwh))
        topup_aud = eligible * tariff.super_export_topup_aud_per_kwh
    return fit_aud, topup_aud


def project_daily_pnl(
    tariff: Tariff,
    *,
    in_window_export_kwh: float,
    out_of_window_evening_export_kwh: float = 0.0,
    daytime_export_kwh: float = 0.0,
    peak_import_kwh: float = 0.0,
    shoulder_import_kwh: float = 0.0,
    offpeak_import_kwh: float = 0.0,
    credit_secured: bool = True,
) -> DailyPnL:
    """Project (or score) a day's dollars from its energy flows."""
    pnl = DailyPnL(supply_charge_aud=tariff.daily_supply_charge_aud)

    peak_rate = tariff.import_rate(time(19, 0))
    shoulder_rate = tariff.import_rate(time(2, 0))
    offpeak_rate = tariff.import_rate(time(12, 0))
    pnl.import_cost_aud = (
        peak_import_kwh * peak_rate
        + shoulder_import_kwh * shoulder_rate
        + offpeak_import_kwh * offpeak_rate
    )
    if peak_import_kwh:
        pnl.breakdown.append(f"{peak_import_kwh:.2f} kWh peak @ ${peak_rate:.3f}")
    if shoulder_import_kwh:
        pnl.breakdown.append(f"{shoulder_import_kwh:.2f} kWh shoulder @ ${shoulder_rate:.3f}")
    if offpeak_import_kwh:
        pnl.breakdown.append(f"{offpeak_import_kwh:.2f} kWh offpeak @ ${offpeak_rate:.3f} (free)")

    if credit_secured:
        pnl.zerohero_credit_aud = tariff.zerohero_credit_aud

    # In-window export: evening FiT plus the Super Export top-up, capped.
    fit_in, topup_in = value_export(tariff, kwh=in_window_export_kwh, at=time(19, 0))
    pnl.fit_revenue_aud += fit_in
    pnl.super_export_topup_aud += topup_in

    # Export between 16:00-18:00 and 21:00-23:00: FiT only, no top-up.
    fit_out, _ = value_export(tariff, kwh=out_of_window_evening_export_kwh, at=time(22, 0))
    pnl.fit_revenue_aud += fit_out

    # Export 23:00-16:00 pays nothing at all on this plan.
    fit_day, _ = value_export(tariff, kwh=daytime_export_kwh, at=time(12, 0))
    pnl.fit_revenue_aud += fit_day
    if daytime_export_kwh > 0.1:
        pnl.breakdown.append(
            f"{daytime_export_kwh:.2f} kWh exported 23:00-16:00 earned $0.00 — "
            f"shift it into 18:00-21:00 to earn $0.10/kWh"
        )

    return pnl


def best_case_daily_pnl(tariff: Tariff) -> DailyPnL:
    """The target the controller is aiming at: compliant, cap-filling, zero paid import."""
    return project_daily_pnl(
        tariff,
        in_window_export_kwh=tariff.super_export_cap_kwh,
        credit_secured=True,
    )


def free_window_recharge_capacity_kwh(
    tariff: Tariff, max_charge_kw: float, charge_efficiency: float = 1.0
) -> float:
    """How much free energy the 11:00-14:00 window can actually deliver to the pack."""
    base = datetime(2000, 1, 1)
    start = base.replace(hour=tariff.free_charge_start.hour, minute=tariff.free_charge_start.minute)
    end = base.replace(hour=tariff.free_charge_end.hour, minute=tariff.free_charge_end.minute)
    if end <= start:
        end += timedelta(days=1)
    hours = (end - start).total_seconds() / 3600.0
    return hours * max_charge_kw * charge_efficiency
