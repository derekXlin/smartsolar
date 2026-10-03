"""The core optimiser: decide how hard to discharge during 18:00-21:00.

THE PHYSICS OF SIMULTANEOUS SOLAR + LOAD + BATTERY UNDER A 10 kW INVERTER LIMIT
==============================================================================

The site obeys one power balance at every instant:

        solar_kw + battery_kw  =  load_kw + export_kw                        (1)

with battery_kw > 0 meaning discharge. Rearranged into the quantity the retailer
actually meters:

        grid_kw = load_kw - solar_kw - battery_kw                            (2)
        (grid_kw > 0 is import, which is what destroys the $1 credit)

Constraint A - the ZeroHero rule.
    We need grid_kw <= 0 essentially all the time, i.e.

        battery_kw >= load_kw - solar_kw          (call this the NET LOAD)     (3)

    The net load is what the battery *must* cover. Note it can be negative early on
    a summer evening, when the array is still making more than the house is using;
    then the battery is not needed at all and the site exports PV directly.

Constraint B - the hybrid inverter's 10 kW AC limit.
    On a hybrid, PV and battery share one AC port, so the limit applies to their sum:

        solar_kw + battery_kw <= 10 kW                                        (4)

    Substituting (1) into (4) gives the equivalent, and more intuitive, form:

        load_kw + export_kw <= 10 kW                                          (5)

    This is the trap that catches naive controllers. Solar does NOT give you extra
    export headroom on a hybrid - it *competes* for the same 10 kW. At 18:00 in
    January with 4 kW of PV still coming in, the battery can only contribute 6 kW,
    no matter how full it is. What solar does buy you is battery *energy*: every kW
    of PV is a kW the battery does not have to supply, so the pack drains slower.

    (If PV is on a separate AC-coupled inverter, set inverter.solar_shares_ac_limit
    to False and (4) becomes battery_kw <= battery_max_discharge_kw instead.)

Constraint C - the DNSP export limit.
    export_kw <= grid_export_limit_kw. Combining with (1):

        battery_kw <= grid_export_limit_kw + load_kw - solar_kw               (6)

Putting A, B and C together, the admissible battery power in any slot is the interval

        max(0, net_load + margin)  <=  battery_kw  <=  B_max

        B_max = min( battery_max_discharge_kw,
                     inverter_ac_limit_kw - solar_kw,        [if hybrid]
                     grid_export_limit_kw + load_kw - solar_kw )               (7)

Everything below is bookkeeping on top of (3) and (7): integrate the lower bound to
get the MANDATORY energy that buys the $1 credit, integrate the gap between the
bounds to get the export CAPACITY, then decide how much of that capacity to actually
use by comparing what a stored kWh is worth against what an exported kWh is worth.

Constraint D - energy, not just power.
    Discharging at the AC meter costs more than that at the cells, because of
    conversion losses:

        dc_drawn_kwh = ac_delivered_kwh / discharge_efficiency                 (8)

    and the pack must not cross its reserve floor:

        dc_drawn_kwh <= (soc_now - reserve_soc)/100 * usable_capacity_kwh      (9)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .config import AppConfig
from .curves import KwAt
from .economics import project_daily_pnl
from .models import (
    AllocationShape,
    BatteryMode,
    Decision,
    ObjectiveMode,
    RiskLevel,
    SlotPlan,
    Telemetry,
    combine_date_time,
    energy_to_soc,
    soc_to_energy,
)
from .tariff import marginal_value_of_stored_energy

log = logging.getLogger(__name__)


class DecisionEngine:
    """Builds an evening discharge plan. Pure and synchronous — easy to unit test."""

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config

    # ------------------------------------------------------------------ windows
    def window_bounds(self, reference: datetime) -> tuple[datetime, datetime]:
        """The 18:00-21:00 credit window on the date of ``reference``."""
        start = combine_date_time(reference, self.cfg.plan.credit_window_start)
        end = combine_date_time(reference, self.cfg.plan.credit_window_end)
        if end <= start:  # window wraps midnight (not the case by default)
            end += timedelta(days=1)
        return start, end

    def overnight_hours(self, window_end: datetime) -> float:
        """Hours from window close until the next free-charge window opens."""
        if self.cfg.strategy.overnight_hours > 0:
            return self.cfg.strategy.overnight_hours
        nxt = combine_date_time(window_end, self.cfg.plan.free_charge_start)
        if nxt <= window_end:
            nxt += timedelta(days=1)
        return (nxt - window_end).total_seconds() / 3600.0

    # ------------------------------------------------------------------- limits
    def _battery_power_ceiling(self, solar_kw: float, load_kw: float) -> float:
        """B_max from equation (7) above."""
        ceiling = self.cfg.battery.max_discharge_kw
        if self.cfg.inverter.solar_shares_ac_limit:
            # (4): PV and battery share the hybrid's single AC port.
            ceiling = min(ceiling, self.cfg.inverter.ac_limit_kw - solar_kw)
        # (6): the grid connection itself will not accept unlimited export.
        ceiling = min(ceiling, self.cfg.inverter.grid_export_limit_kw + load_kw - solar_kw)
        return max(0.0, ceiling)

    # --------------------------------------------------------------------- plan
    def plan(
        self,
        *,
        now: datetime,
        telemetry: Telemetry,
        solar_kw_at: KwAt,
        load_kw_at: KwAt,
        degraded: bool = False,
        notes: list[str] | None = None,
        morning_solar_to_battery_kwh: float | None = None,
        already_exported_kwh: float = 0.0,
        overnight_need_kwh: float | None = None,
        overnight_note: str | None = None,
    ) -> Decision:
        cfg = self.cfg
        rationale: list[str] = list(notes or [])

        window_start, window_end = self.window_bounds(now)
        plan_start = max(now, window_start)
        if plan_start >= window_end:
            # Asked to plan after the window closed — plan tomorrow's instead.
            window_start += timedelta(days=1)
            window_end += timedelta(days=1)
            plan_start = window_start
            rationale.append("current window already closed; planning for tomorrow")

        slots = self._build_slots(plan_start, window_end, solar_kw_at, load_kw_at)

        mandatory_ac_kwh = sum(s.mandatory_discharge_kw * s.hours for s in slots)
        mandatory_dc_kwh = mandatory_ac_kwh / cfg.battery.discharge_efficiency
        export_capacity_kwh = sum(s.export_headroom_kw * s.hours for s in slots)
        passive_export_kwh = sum(
            max(0.0, s.solar_kw + s.mandatory_discharge_kw - s.load_kw) * s.hours for s in slots
        )

        # ---- energy budget, per constraint D --------------------------------
        capacity = cfg.battery.usable_capacity_kwh
        e_now = soc_to_energy(telemetry.soc_pct, capacity)
        e_reserve = soc_to_energy(cfg.battery.min_reserve_soc_pct, capacity)
        e_floor = soc_to_energy(cfg.battery.emergency_floor_soc_pct, capacity)

        risk = RiskLevel.OK
        effective_reserve = e_reserve

        if mandatory_dc_kwh > e_now - e_reserve:
            if cfg.battery.allow_reserve_breach_for_credit and mandatory_dc_kwh <= e_now - e_floor:
                effective_reserve = e_floor
                risk = RiskLevel.WATCH
                rationale.append(
                    f"mandatory {mandatory_dc_kwh:.1f} kWh exceeds the planning reserve; "
                    f"allowing the pack down to the {cfg.battery.emergency_floor_soc_pct:.0f}% "
                    f"hard floor to protect the ${cfg.plan.daily_credit_aud:.0f} credit"
                )
            else:
                risk = RiskLevel.AT_RISK
                rationale.append(
                    f"INSUFFICIENT ENERGY: need {mandatory_dc_kwh:.1f} kWh to hold import at zero "
                    f"but only {max(0.0, e_now - e_floor):.1f} kWh is available above the hard floor"
                )

        spare_dc = max(0.0, e_now - effective_reserve - mandatory_dc_kwh - cfg.strategy.energy_safety_buffer_kwh)

        export_budget_ac, retention_kwh, econ_notes = self._export_budget(
            spare_dc_kwh=spare_dc,
            e_now=e_now,
            mandatory_dc_kwh=mandatory_dc_kwh,
            reserve_kwh=effective_reserve,
            window_end=window_end,
            risk=risk,
            morning_solar_kwh=morning_solar_to_battery_kwh,
            overnight_need_kwh=overnight_need_kwh,
            overnight_note=overnight_note,
        )
        rationale.extend(econ_notes)

        # Super Export pays its top-up only on roughly the first 15 kWh in-window.
        # Beyond the cap an exported kWh drops from $0.10 to $0.02 — well below the
        # $0.407 it is worth sitting in the battery — so we stop there by default.
        if cfg.strategy.limit_to_super_export_cap:
            # On a mid-window replan, energy already pushed out has consumed part of
            # the cap. Without this the plan would keep authorising a fresh 15 kWh
            # every replan and quietly sell down the pack at $0.02 instead of $0.10.
            cap_left = max(
                0.0,
                cfg.plan.super_export_cap_kwh - passive_export_kwh - already_exported_kwh,
            )
            if export_budget_ac > cap_left:
                rationale.append(
                    f"trimmed export to the {cfg.plan.super_export_cap_kwh:.0f} kWh Super Export cap "
                    f"({passive_export_kwh:.1f} kWh of it is passive PV export)"
                )
                export_budget_ac = cap_left

        if export_budget_ac > export_capacity_kwh:
            rationale.append(
                f"power-limited: only {export_capacity_kwh:.1f} kWh of export fits inside the "
                f"{cfg.inverter.ac_limit_kw:.0f} kW inverter limit over the remaining window"
            )
            export_budget_ac = export_capacity_kwh

        self._allocate_export(slots, export_budget_ac)

        # ---- hard energy floor, per constraint D -----------------------------
        # Walk the plan forward and stop the battery the instant it would cross the
        # emergency floor. Without this the planner happily writes a profile that ends
        # at 0% SOC, which no real BMS would follow and which would leave the house
        # importing at the $0.528 peak rate for the rest of the evening.
        shortfall = self._enforce_energy_floor(slots, e_now, e_floor)
        credit_achievable = shortfall <= cfg.strategy.unwinnable_margin_kwh
        recommended_mode = BatteryMode.FORCE_EXPORT

        if not credit_achievable:
            risk = RiskLevel.AT_RISK
            if cfg.strategy.abandon_credit_if_unwinnable:
                # The ZeroHero rule needs EVERY hour of 18:00-21:00 to comply, so a
                # partial effort earns nothing. Stop selling, stop draining to the
                # floor, and let the battery serve load normally down to the planning
                # reserve — that energy is worth $0.528/kWh against the evening peak
                # and $0.407/kWh against the morning shoulder, which beats spending it
                # on a credit we cannot win.
                for s_ in slots:
                    s_.export_discharge_kw = 0.0
                # How far down should self-consumption be allowed to go? Compare the
                # price of the import we avoid NOW against the price of the import we
                # would avoid later by hoarding. Evening peak is $0.528 and the
                # overnight blend is ~$0.424, so spending the charge tonight wins and
                # we clamp to the hard floor rather than the (comfort-driven) planning
                # reserve. If the rates ever invert, this flips automatically.
                evening_rate = cfg.plan.tariff.import_rate(cfg.plan.credit_window_start)
                overnight_rate = cfg.plan.tariff.blended_import_rate(
                    cfg.plan.credit_window_end, self.overnight_hours(window_end)
                )
                abandon_floor = e_floor if evening_rate > overnight_rate else e_reserve
                self._enforce_energy_floor(slots, e_now, abandon_floor)
                recommended_mode = BatteryMode.SELF_CONSUMPTION
                rationale.append(
                    f"ABANDONING the ${cfg.plan.daily_credit_aud:.0f} credit: short by "
                    f"{shortfall:.1f} kWh and the rule needs every hour to comply. "
                    f"Reverting to self-consumption down to "
                    f"{energy_to_soc(abandon_floor, capacity):.0f}% — evening import costs "
                    f"${evening_rate:.3f}/kWh vs ${overnight_rate:.3f}/kWh overnight, so the "
                    f"remaining charge is worth more spent tonight than hoarded."
                )
            else:
                rationale.append(
                    f"credit unwinnable (short {shortfall:.1f} kWh) but "
                    f"abandon_credit_if_unwinnable=False — discharging to the hard floor anyway"
                )

        # ---- how the loop will run it ----------------------------------------
        forced = [s for s in slots if s.export_discharge_kw >= cfg.strategy.min_force_export_kw]
        if recommended_mode is BatteryMode.FORCE_EXPORT and forced:
            rationale.append(
                f"control: force-discharge {forced[0].start:%H:%M}-{forced[-1].end:%H:%M} "
                f"exporting {sum(s.export_discharge_kw * s.hours for s in forced):.1f} kWh, "
                f"self-use for the rest of the window"
            )
        else:
            rationale.append("control: self-use for the whole window — the inverter follows the load")

        # ---- roll up ---------------------------------------------------------
        total_ac_kwh = sum(s.battery_ac_kw * s.hours for s in slots)
        total_dc_kwh = total_ac_kwh / cfg.battery.discharge_efficiency
        total_export_kwh = sum(s.grid_export_kw * s.hours for s in slots)
        total_hours = sum(s.hours for s in slots) or 1.0
        final_energy = max(0.0, e_now - total_dc_kwh)

        # ---- dollars: the actual objective ----------------------------------
        # Any energy we fail to supply during the window is imported at the $0.528
        # peak rate, on top of losing the $1 credit — so the P&L captures both halves
        # of a shortfall, not just the missing credit.
        pnl = project_daily_pnl(
            cfg.plan.tariff,
            in_window_export_kwh=total_export_kwh,
            peak_import_kwh=max(0.0, shortfall),
            credit_secured=credit_achievable,
        )

        decision = Decision(
            made_at=now,
            window_start=window_start,
            window_end=window_end,
            starting_soc_pct=telemetry.soc_pct,
            starting_energy_kwh=e_now,
            target_export_kwh=total_export_kwh,
            mandatory_discharge_kwh=mandatory_ac_kwh,
            opportunistic_export_kwh=sum(s.export_discharge_kw * s.hours for s in slots),
            passive_solar_export_kwh=passive_export_kwh,
            recommended_discharge_kw=total_ac_kwh / total_hours,
            peak_discharge_kw=max((s.battery_ac_kw for s in slots), default=0.0),
            expected_final_soc=energy_to_soc(final_energy, capacity),
            expected_final_energy_kwh=final_energy,
            battery_dc_drawn_kwh=total_dc_kwh,
            reserve_floor_soc_pct=energy_to_soc(effective_reserve, capacity),
            overnight_retention_kwh=retention_kwh,
            slots=slots,
            credit_achievable=credit_achievable,
            recommended_mode=recommended_mode,
            energy_shortfall_kwh=shortfall,
            expected_net_aud=pnl.net_aud,
            pnl_summary=pnl.format(),
            risk=risk,
            degraded=degraded,
            rationale=rationale,
            inputs=self._input_snapshot(telemetry, slots, window_start, window_end),
        )
        return decision

    # ------------------------------------------------------------------- slots
    def _build_slots(
        self, start: datetime, end: datetime, solar_kw_at: KwAt, load_kw_at: KwAt
    ) -> list[SlotPlan]:
        step = timedelta(minutes=self.cfg.strategy.slot_minutes)
        margin = self.cfg.strategy.import_safety_margin_kw
        slots: list[SlotPlan] = []
        t = start
        while t < end:
            t_end = min(t + step, end)
            mid = t + (t_end - t) / 2
            solar = max(0.0, solar_kw_at(mid))
            load = max(0.0, load_kw_at(mid))

            ceiling = self._battery_power_ceiling(solar, load)
            # Equation (3) plus a deliberate margin so the meter sees a small export
            # rather than hovering at exactly zero, where noise could tip us to import.
            mandatory = min(ceiling, max(0.0, load - solar + margin))
            headroom = max(0.0, ceiling - mandatory)

            slots.append(
                SlotPlan(
                    start=t,
                    end=t_end,
                    solar_kw=solar,
                    load_kw=load,
                    mandatory_discharge_kw=mandatory,
                    export_discharge_kw=0.0,
                    export_headroom_kw=headroom,
                )
            )
            t = t_end
        return slots

    def _enforce_energy_floor(self, slots: list[SlotPlan], e_now: float, floor_kwh: float) -> float:
        """Clip the planned profile so the pack never crosses ``floor_kwh``.

        Returns the AC energy we wanted but could not deliver — the shortfall that
        determines whether the credit is winnable at all.
        """
        eff = self.cfg.battery.discharge_efficiency
        available_dc = max(0.0, e_now - floor_kwh)
        shortfall_ac = 0.0

        for s in slots:
            want_ac = s.battery_ac_kw * s.hours
            want_dc = want_ac / eff
            if want_dc <= available_dc:
                available_dc -= want_dc
                continue
            # Partially fund this slot, then zero everything after it.
            afford_ac = available_dc * eff
            scale = afford_ac / want_ac if want_ac > 0 else 0.0
            s.mandatory_discharge_kw *= scale
            s.export_discharge_kw *= scale
            shortfall_ac += want_ac - afford_ac
            available_dc = 0.0
        return shortfall_ac

    # ---------------------------------------------------------------- economics
    def _export_budget(
        self,
        *,
        spare_dc_kwh: float,
        e_now: float,
        mandatory_dc_kwh: float,
        reserve_kwh: float,
        window_end: datetime,
        risk: RiskLevel,
        morning_solar_kwh: float | None = None,
        overnight_need_kwh: float | None = None,
        overnight_note: str | None = None,
    ) -> tuple[float, float, list[str]]:
        """Decide how much AC energy to dedicate to opportunistic export."""
        cfg = self.cfg
        notes: list[str] = []
        eff = cfg.battery.discharge_efficiency

        if risk is RiskLevel.AT_RISK or spare_dc_kwh <= 0:
            notes.append("no spare energy above reserve — mandatory discharge only")
            return 0.0, max(0.0, e_now - mandatory_dc_kwh), notes

        hours = self.overnight_hours(window_end)
        overnight_need = hours * cfg.strategy.overnight_load_kw / eff

        if cfg.strategy.objective is ObjectiveMode.GUARANTEE_CREDIT_ONLY:
            notes.append("objective=guarantee_credit_only — holding all surplus in the battery")
            return 0.0, max(0.0, e_now - mandatory_dc_kwh), notes

        if cfg.strategy.objective is ObjectiveMode.MAXIMISE_EXPORT:
            notes.append("objective=maximise_export — selling everything above the planning reserve")
            return spare_dc_kwh * eff, reserve_kwh, notes

        if cfg.strategy.objective is ObjectiveMode.RETAIN_OVERNIGHT:
            if overnight_need_kwh is not None:
                # Battery energy to the sunrise low point, from the learned drain and
                # tomorrow's sun (overnight.py): already in battery terms.
                overnight_need = overnight_need_kwh
                notes.append(f"objective=retain_overnight — {overnight_note or f'holding {overnight_need:.1f} kWh for the night'}")
            else:
                notes.append(
                    f"objective=retain_overnight — holding {overnight_need:.1f} kWh for the "
                    f"{hours:.1f} h until the free window opens"
                )
            budget_dc = max(0.0, spare_dc_kwh - overnight_need)
            return budget_dc * eff, reserve_kwh + overnight_need, notes

        # ---- ECONOMIC: compare what a kWh is worth kept vs sold ---------------
        # Free charging can push in up to (charge power x window length) kWh at
        # zero cost tomorrow, so energy occupying space we could refill for free is
        # worth only its export price.
        free_hours = (
            datetime.combine(window_end.date(), cfg.plan.free_charge_end)
            - datetime.combine(window_end.date(), cfg.plan.free_charge_start)
        ).total_seconds() / 3600.0
        free_recharge = free_hours * cfg.battery.max_charge_kw * cfg.battery.charge_efficiency

        energy_if_no_export = e_now - mandatory_dc_kwh
        # Value retained energy at the blended price of the import it displaces
        # between window close and the next free window (peak 21:00-23:00, then shoulder).
        blended_import = cfg.plan.tariff.blended_import_rate(cfg.plan.credit_window_end, hours)
        export_rate = cfg.plan.in_window_export_rate

        valuation = marginal_value_of_stored_energy(
            energy_at_window_close_kwh=energy_if_no_export,
            usable_capacity_kwh=cfg.battery.usable_capacity_kwh,
            overnight_consumption_kwh=hours * cfg.strategy.overnight_load_kw,
            morning_solar_to_battery_kwh=(
                cfg.strategy.morning_solar_to_battery_kwh
                if morning_solar_kwh is None
                else morning_solar_kwh
            ),
            free_window_recharge_kwh=free_recharge,
            reserve_floor_kwh=reserve_kwh,
            blended_import_rate_aud_per_kwh=blended_import,
            export_rate_aud_per_kwh=export_rate,
        )
        notes.extend(valuation.reasons)

        if export_rate >= blended_import:
            # Only possible under an odd tariff, but handle it honestly.
            notes.append("export rate beats the import it would displace — selling the surplus")
            budget_dc = spare_dc_kwh
        else:
            budget_dc = min(spare_dc_kwh, valuation.stranded_kwh)
            notes.append(
                f"objective=economic — selling {budget_dc:.1f} kWh of stranded energy at "
                f"${export_rate:.2f}/kWh and keeping the rest, which is worth "
                f"${blended_import:.3f}/kWh as avoided import"
            )

        retention = max(reserve_kwh, energy_if_no_export - budget_dc)
        return budget_dc * eff, retention, notes

    # ---------------------------------------------------------------- allocation
    def _allocate_export(self, slots: list[SlotPlan], budget_ac_kwh: float) -> None:
        """Spread ``budget_ac_kwh`` of extra export across the slots.

        Water-filling: hand out energy in proportion to the shape weights, clip any
        slot that hits its power headroom, then redistribute the leftover among the
        slots that still have room. Repeats until the budget is placed or every slot
        is saturated.
        """
        if budget_ac_kwh <= 1e-9 or not slots:
            return

        if self.cfg.strategy.allocation is AllocationShape.BLOCK:
            # Fill slots in time order at their full headroom until the budget runs out.
            remaining = budget_ac_kwh
            for s in slots:
                take = min(remaining, s.export_headroom_kw * s.hours)
                s.export_discharge_kw = take / s.hours if s.hours > 0 else 0.0
                remaining -= take
                if remaining <= 1e-9:
                    break
            return

        weights = self._shape_weights(slots)
        caps = [s.export_headroom_kw * s.hours for s in slots]
        alloc = [0.0] * len(slots)
        remaining = budget_ac_kwh

        for _ in range(64):
            open_idx = [i for i in range(len(slots)) if caps[i] - alloc[i] > 1e-9]
            if not open_idx or remaining <= 1e-9:
                break
            total_w = sum(weights[i] * slots[i].hours for i in open_idx)
            if total_w <= 0:
                break
            placed_any = False
            for i in open_idx:
                share = remaining * (weights[i] * slots[i].hours) / total_w
                take = min(share, caps[i] - alloc[i])
                if take > 0:
                    alloc[i] += take
                    placed_any = True
            newly_remaining = budget_ac_kwh - sum(alloc)
            if not placed_any or abs(newly_remaining - remaining) < 1e-12:
                remaining = newly_remaining
                break
            remaining = newly_remaining

        for s, a in zip(slots, alloc, strict=True):
            s.export_discharge_kw = a / s.hours if s.hours > 0 else 0.0

    def _shape_weights(self, slots: list[SlotPlan]) -> list[float]:
        shape = self.cfg.strategy.allocation
        n = len(slots)
        if shape is AllocationShape.CONSTANT:
            return [1.0] * n
        if shape is AllocationShape.FRONT_LOADED:
            # Bank the Super Export kWh early: if the evening turns out worse than
            # forecast (cloud, a surprise 4 kW oven), the revenue is already in the
            # meter and only the mandatory reserve is left to defend.
            if n == 1:
                return [1.0]
            return [2.0 - 1.5 * (i / (n - 1)) for i in range(n)]
        # SOLAR_FOLLOWING: export harder while PV is still contributing, so the pack
        # drains at a steadier rate across the window.
        return [0.3 + s.solar_kw for s in slots]

    # ------------------------------------------------------------------ snapshot
    def _input_snapshot(
        self, telemetry: Telemetry, slots: list[SlotPlan], start: datetime, end: datetime
    ) -> dict[str, Any]:
        return {
            "soc_pct": telemetry.soc_pct,
            "battery_energy_kwh": round(telemetry.battery_energy_kwh, 2),
            "solar_kw_now": round(telemetry.solar_kw, 2),
            "load_kw_now": round(telemetry.load_kw, 2),
            "window": f"{start:%H:%M}-{end:%H:%M}",
            "forecast_solar_kwh_in_window": round(sum(s.solar_kw * s.hours for s in slots), 2),
            "forecast_load_kwh_in_window": round(sum(s.load_kw * s.hours for s in slots), 2),
            "objective": self.cfg.strategy.objective.value,
            "allocation": self.cfg.strategy.allocation.value,
        }
