"""The live control loop: turn a Decision into second-by-second inverter setpoints.

The plan built at 17:50 is a forecast. Reality diverges: a cloud arrives, the oven
goes on, the forecast was optimistic. So the loop runs a closed loop on the meter
rather than blindly replaying the plan.

THE SETPOINT EQUATION
---------------------
From the site power balance (see decision_engine), holding grid import at zero needs

    battery_kw >= load_kw - solar_kw

but we do not trust our own load and solar readings to be perfectly simultaneous or
perfectly calibrated. The grid meter IS the ground truth — it is literally the number
the retailer bills on. So the loop uses it directly as a feedback term:

    error_kw   = grid_kw - (-planned_export_kw)      # how far off target we are
    setpoint   = current_battery_kw + error_kw       # gain of exactly 1

The gain-of-1 is not a tuning choice, it is physics: adding 1 kW of battery discharge
removes exactly 1 kW of grid import. That makes the loop deadbeat in one step, with no
overshoot to tune, provided the inverter can follow.

On top of that sit three protections:
  1. an import-safety margin that keeps the meter at a small export rather than at
     exactly zero, so measurement noise cannot tip a sample into import;
  2. an escalation of that margin as the current hour's 0.03 kWh allowance is consumed;
  3. an energy guard that abandons opportunistic export the moment the remaining
     charge stops comfortably covering the rest of the window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .clock import Clock
from .config import AppConfig
from .controllers.base import BatteryController, SafetyWrapper
from .credit_monitor import CreditMonitor
from .curves import ForecastCurve, KwAt, constant_curve
from .data_providers.base import ForecastProvider, ProviderError, TelemetryProvider
from .decision_engine import DecisionEngine
from .economics import project_daily_pnl
from .ledger import Ledger
from .models import (
    BatteryMode,
    ControlCommand,
    DailyOutcome,
    Decision,
    SlotPlan,
    Telemetry,
)

log = logging.getLogger(__name__)


class EveningRunner:
    """Owns one evening: decide at 17:50, control until 21:00, then write the ledger."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        telemetry: TelemetryProvider,
        forecast: ForecastProvider,
        controller: BatteryController,
        ledger: Ledger | None,
        clock: Clock,
    ) -> None:
        self.cfg = cfg
        self.telemetry = telemetry
        self.forecast = forecast
        self.controller = controller
        self.ledger = ledger
        self.clock = clock
        self.engine = DecisionEngine(cfg)

        self.decision: Decision | None = None
        self.monitor: CreditMonitor | None = None
        self.exported_kwh = 0.0
        self.manual_override_kw: float | None = None
        self.last_telemetry: Telemetry | None = None
        self.last_setpoint_kw = 0.0
        self.degraded = False
        self.status_note = "idle"

    # ------------------------------------------------------------- forecasting
    async def _curves(self, start: datetime, end: datetime) -> tuple[KwAt, KwAt, bool]:
        """Return (solar_kw_at, load_kw_at, degraded)."""
        degraded = False
        try:
            solar_pts = await self.forecast.solar(start, end)
            solar: KwAt = ForecastCurve(solar_pts, "solar_kw") if solar_pts else constant_curve(0.0)
            if not solar_pts:
                degraded = True
        except Exception as exc:  # noqa: BLE001
            log.warning("solar forecast failed: %s — assuming zero residual solar", exc)
            # Assuming zero solar is the SAFE direction: it makes the planner reserve
            # more battery than it probably needs, which costs a little SOC but
            # cannot cost the credit.
            solar = constant_curve(0.0)
            degraded = True

        try:
            load_pts = await self.forecast.load(start, end)
            load: KwAt = (
                ForecastCurve(load_pts, "load_kw")
                if load_pts
                else constant_curve(self.cfg.forecast.static_evening_load_kw)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("load forecast failed: %s — using the static evening average", exc)
            load = constant_curve(self.cfg.forecast.static_evening_load_kw)
            degraded = True
        return solar, load, degraded

    # ---------------------------------------------------------------- decision
    async def make_decision(self, *, now: datetime | None = None) -> Decision:
        now = now or self.clock.now()
        window_start, window_end = self.engine.window_bounds(now)

        telemetry_assumed = False
        try:
            tel = await self.telemetry.read(now)
        except ProviderError as exc:
            # Loud, and named. A stand-in SOC looks exactly like a real one in the
            # output, and a plan built on a guessed battery state can abandon a
            # winnable credit or over-export a pack that was never that full.
            log.error("NO TELEMETRY at decision time (%s) — assuming %.0f%% SOC. "
                      "Every figure below is a guess about the battery.",
                      exc, self.cfg.battery.min_reserve_soc_pct + 10.0)
            tel = self._assumed_telemetry(now)
            self.degraded = True
            telemetry_assumed = True

        solar, load, degraded = await self._curves(min(now, window_start), window_end)
        self.degraded = self.degraded or degraded

        notes = []
        if telemetry_assumed:
            notes.append(
                f"TELEMETRY UNAVAILABLE — SOC was NOT measured. Assumed "
                f"{tel.soc_pct:.0f}% (min_reserve + 10). Treat every figure here as "
                f"a guess until a real reading returns."
            )
        elif self.degraded:
            notes.append("DEGRADED: forecast unavailable, using fallback curves")
        load_note = getattr(self.forecast, "load_note", None)
        if load_note:
            notes.append(load_note)

        decision = self.engine.plan(
            now=now,
            telemetry=tel,
            solar_kw_at=solar,
            load_kw_at=load,
            degraded=self.degraded,
            notes=notes,
            morning_solar_to_battery_kwh=await self._morning_solar_estimate(window_end),
        )
        decision.telemetry_assumed = telemetry_assumed
        self.decision = decision
        self.monitor = CreditMonitor(
            decision.window_start, decision.window_end, self.cfg.plan.import_limit_kwh_per_hour
        )
        if self.ledger:
            self._replay_window_so_far(now)
            self.ledger.record_decision(decision)
        log.info("decision: %s | %s", decision.summary(), decision.pnl_summary)
        for line in decision.rationale:
            log.info("  reason: %s", line)
        return decision

    def _replay_window_so_far(self, now: datetime) -> None:
        """After a mid-window restart, rebuild import and export from the samples log.

        A fresh monitor forgets everything before the restart, so an hour already
        breached would read clean again and the day's export would restart at zero.
        Every tick is already in samples.jsonl; replaying the in-window ones restores
        both, and a restart gap longer than the sample-gap limit stays visible as
        unwatched time rather than being papered over.
        """
        assert self.decision is not None and self.monitor is not None and self.ledger is not None
        start = self.decision.window_start
        if now <= start:
            return
        samples = self.ledger.read_samples(start, now)
        for when, grid_kw, forced in samples:
            self._account(when, grid_kw, forced=forced)
        if samples:
            log.warning("resumed mid-window: replayed %d samples from %s to %s | %s",
                        len(samples), samples[0][0].strftime("%H:%M"),
                        samples[-1][0].strftime("%H:%M"), self.monitor.report())

    def _account(self, when: datetime, grid_kw: float, *, forced: bool = True) -> None:
        """Feed one sample into the credit monitor and the export total.

        ``when`` is the measurement time. Snapshots measured before the window,
        or polled again, are skipped for the same reasons as in CreditMonitor.
        ``forced``: was the battery force-discharging when it was measured.
        """
        if self.monitor:
            self.monitor.observe(when, grid_kw, forced=forced)
        if self.decision is not None and when < self.decision.window_start:
            return
        prev = getattr(self, "_last_export_sample", None)
        if prev is not None and when <= prev[0]:
            return
        export_kw = max(0.0, -grid_kw)
        if grid_kw < 0:
            # Accumulate exported energy between samples.
            prev = getattr(self, "_last_export_sample", None)
            if prev is not None:
                dt_h = (when - prev[0]).total_seconds() / 3600.0
                if 0 < dt_h < 1:
                    self.exported_kwh += (prev[1] + export_kw) / 2 * dt_h
            self._last_export_sample = (when, export_kw)
        else:
            self._last_export_sample = (when, 0.0)

    def _mode_at(self, when: datetime) -> BatteryMode | None:
        """The mode we had commanded when a reading was measured. None if nothing
        had been commanded yet, which the monitor treats as forced: until our first
        write the owner's schedule is in charge, and it may itself force-discharge."""
        for command in reversed(self.controller.command_log):
            if command.timestamp <= when:
                return command.mode
        return None

    async def _morning_solar_estimate(self, window_end: datetime) -> float | None:
        """How much PV will reach the battery between sunrise and the free window.

        Feeds the ECONOMIC objective: a big solar morning means the pack refills
        itself for free, which makes tonight's surplus stranded and therefore worth
        selling. A dull morning means the opposite.
        """
        try:
            nxt = window_end.replace(
                hour=self.cfg.plan.free_charge_start.hour,
                minute=self.cfg.plan.free_charge_start.minute,
                second=0, microsecond=0,
            )
            if nxt <= window_end:
                nxt += timedelta(days=1)
            dawn = nxt - timedelta(hours=6)
            pts = await self.forecast.solar(dawn, nxt)
            if not pts:
                return None
            curve = ForecastCurve(pts, "solar_kw")
            from .curves import integrate

            gross = integrate(curve, dawn, nxt, step_minutes=15)
            # Only the part above the daytime house load can charge the battery.
            daytime_load = self.cfg.forecast.static_evening_load_kw
            hours = (nxt - dawn).total_seconds() / 3600.0
            return max(0.0, gross - daytime_load * hours)
        except Exception as exc:  # noqa: BLE001
            log.debug("morning solar estimate unavailable: %s", exc)
            return None

    def _assumed_telemetry(self, now: datetime) -> Telemetry:
        """Pessimistic stand-in when we are flying blind at decision time."""
        soc = self.cfg.battery.min_reserve_soc_pct + 10.0
        from .models import soc_to_energy

        return Telemetry(
            timestamp=now,
            soc_pct=soc,
            battery_energy_kwh=soc_to_energy(soc, self.cfg.battery.usable_capacity_kwh),
            solar_kw=0.0,
            load_kw=self.cfg.forecast.static_evening_load_kw,
            stale=True,
        )

    # ------------------------------------------------------------------- loop
    def _slot_for(self, when: datetime) -> SlotPlan | None:
        if not self.decision:
            return None
        for s in self.decision.slots:
            if s.start <= when < s.end:
                return s
        return None

    def _margin_kw(self, now: datetime) -> float:
        """Import safety margin, escalated as the hour's allowance is consumed.

        At full headroom we run the configured margin (0.25 kW). As the 0.03 kWh
        budget for the current hour is eaten, the margin ramps up to 3x, pushing the
        meter further into export and buying back certainty. Cheap insurance: even
        at 0.75 kW for a full hour the extra cost is 0.75 kWh of battery, worth about
        $0.32 — against a $1 credit.
        """
        base = self.cfg.strategy.import_safety_margin_kw
        if self.monitor is None:
            return base
        headroom = self.monitor.headroom_fraction(now)
        return base * (1.0 + 2.0 * (1.0 - headroom))

    def _energy_guard(self, tel: Telemetry, now: datetime) -> tuple[bool, str]:
        """Should we stop opportunistic export to protect the rest of the window?"""
        if not self.decision:
            return False, ""
        remaining = [s for s in self.decision.slots if s.end > now]
        if not remaining:
            return False, ""
        needed_ac = sum(s.mandatory_discharge_kw * s.hours for s in remaining)
        needed_dc = needed_ac / self.cfg.battery.discharge_efficiency + self.cfg.strategy.energy_safety_buffer_kwh
        from .models import soc_to_energy

        floor = soc_to_energy(self.cfg.battery.emergency_floor_soc_pct, self.cfg.battery.usable_capacity_kwh)
        available = tel.battery_energy_kwh - floor
        if available < needed_dc:
            return True, (
                f"energy guard: {available:.1f} kWh above floor vs {needed_dc:.1f} kWh "
                f"still needed — suspending opportunistic export"
            )
        return False, ""

    def _ceiling_kw(self, tel: Telemetry) -> float:
        """Live re-evaluation of equation (7) using measured solar and load."""
        cfg = self.cfg
        ceiling = cfg.battery.max_discharge_kw
        if cfg.inverter.solar_shares_ac_limit:
            ceiling = min(ceiling, cfg.inverter.ac_limit_kw - tel.solar_kw)
        ceiling = min(ceiling, cfg.inverter.grid_export_limit_kw + tel.load_kw - tel.solar_kw)
        return max(0.0, ceiling)

    def choose_mode(self, tel: Telemetry | None, now: datetime) -> tuple[BatteryMode, str]:
        """Self-use unless the plan is exporting enough to act as its own buffer.

        Self-use is what protects the credit: the inverter matches the house load
        from its own meter within about a second. Force-discharge holds a fixed
        power, leaves anything above it to the grid, and we only see the house
        through a cloud feed minutes old. The first live evening force-discharged
        at load + 0.25 kW for three hours and lost 18:00 to a 2 kW load step; the
        owner's own schedule (full-power export 18:00-19:00, then self-use) had
        secured the credit every day.

        So force-discharge happens only while the planned export is at least
        min_force_export_kw: then a load spike just trims the export.
        """
        if self.manual_override_kw is not None:
            return BatteryMode.FORCE_EXPORT, "manual override"
        if self.decision is None or self.decision.recommended_mode is not BatteryMode.FORCE_EXPORT:
            return BatteryMode.SELF_CONSUMPTION, "self-use: credit abandoned, battery serves the house"
        slot = self._slot_for(now)
        export_kw = slot.export_discharge_kw if slot else 0.0
        if export_kw < self.cfg.strategy.min_force_export_kw:
            return BatteryMode.SELF_CONSUMPTION, "self-use: the inverter follows the load itself"
        if tel is not None and not tel.stale:
            suspend, guard_note = self._energy_guard(tel, now)
            if suspend:
                return BatteryMode.SELF_CONSUMPTION, f"self-use: {guard_note}"
        return BatteryMode.FORCE_EXPORT, f"exporting {export_kw:.1f} kW"

    def compute_setpoint(self, tel: Telemetry, now: datetime) -> tuple[float, str]:
        """The force-discharge control law. Returns (battery_kw, reason)."""
        if self.manual_override_kw is not None:
            return self.manual_override_kw, "manual override"

        if tel.stale:
            # Flying blind: fall back to the simple, documented behaviour — force
            # export at a fixed rate. It will not be optimal, but it is predictable
            # and it keeps the battery pushing against the house load. Never BELOW
            # what we were already commanding or what the plan wants, though: the
            # last setpoint answered the last load we saw, and dropping it on no
            # new information is how 18:55 on the first live evening cut 4.75 kW
            # to 3.0 kW while the house was still drawing 4 kW.
            slot = self._slot_for(now)
            planned = slot.battery_ac_kw if slot else 0.0
            hold = max(self.last_setpoint_kw, planned)
            fallback = self.cfg.strategy.fallback_discharge_kw
            if hold > fallback:
                return min(hold, self._ceiling_kw(tel)), f"FALLBACK: telemetry stale, holding {hold:.2f} kW"
            return fallback, "FALLBACK: telemetry stale"

        slot = self._slot_for(now)
        planned_export_kw = slot.export_discharge_kw if slot else 0.0

        suspend, guard_note = self._energy_guard(tel, now)
        if suspend:
            planned_export_kw = 0.0

        margin = self._margin_kw(now)

        # Feed-forward from the measured net load, plus the deliberate margin.
        feed_forward = max(0.0, tel.net_load_kw) + margin + planned_export_kw

        # Closed-loop correction against the meter, gain = 1 (see module docstring).
        target_grid_kw = -(margin + planned_export_kw)
        error = tel.grid_kw - target_grid_kw
        closed_loop = tel.battery_kw + error

        # Take the larger: the feed-forward gets us in the right neighbourhood
        # immediately after a step change, the closed loop removes steady-state bias.
        setpoint = max(feed_forward, closed_loop)
        setpoint = min(setpoint, self._ceiling_kw(tel))
        setpoint = max(0.0, setpoint)

        reason = guard_note or (
            f"net {tel.net_load_kw:+.2f} kW, margin {margin:.2f} kW, "
            f"export {planned_export_kw:.2f} kW"
        )
        return setpoint, reason

    async def tick(self, now: datetime | None = None) -> Telemetry | None:
        """One control iteration."""
        now = now or self.clock.now()
        try:
            tel = await self.telemetry.read(now)
        except ProviderError as exc:
            log.error("telemetry unavailable: %s — holding last setpoint", exc)
            self.status_note = f"telemetry failure: {exc}"
            return None

        self.last_telemetry = tel
        if isinstance(self.controller, SafetyWrapper):
            self.controller.observe_soc(tel.soc_pct)
        measured_mode = self._mode_at(tel.observed_time)
        self._account(tel.observed_time, tel.grid_kw,
                      forced=measured_mode not in (BatteryMode.SELF_CONSUMPTION, BatteryMode.HOLD))

        mode, reason = self.choose_mode(tel, now)
        if mode is BatteryMode.FORCE_EXPORT:
            setpoint, reason = self.compute_setpoint(tel, now)
        else:
            setpoint = 0.0

        last = self.controller.last_command
        changed = (
            last is None
            or last.mode is not mode
            or abs(last.power_kw - setpoint) >= self.cfg.controller.command_deadband_kw
        )
        if (
            changed and last is not None and last.mode is mode and setpoint < last.power_kw
            and (now - last.timestamp).total_seconds() < self.cfg.controller.min_lower_interval_seconds
        ):
            # Asymmetric pacing: see ControllerConfig.min_lower_interval_seconds.
            changed = False
        if changed:
            await self.controller.apply(
                ControlCommand(timestamp=now, mode=mode, power_kw=setpoint, reason=reason)
            )
        self.last_setpoint_kw = setpoint
        self.status_note = reason
        if self.ledger:
            self.ledger.record_sample(tel, setpoint, reason,
                                      mode=measured_mode.value if measured_mode else None)
        return tel

    async def run_window(self) -> DailyOutcome:
        """Decide, control through the window, then close out and score the day."""
        decision = self.decision or await self.make_decision()
        interval = self.cfg.strategy.control_interval_seconds
        replan_every = max(1, self.cfg.strategy.replan_minutes * 60 // interval)

        now = self.clock.now()
        if now < decision.window_start and self.controller.capabilities().window_bounded:
            await self._prearm(decision, now)
        while now < decision.window_start:
            await self.clock.sleep(min(interval, (decision.window_start - now).total_seconds()))
            now = self.clock.now()

        log.info("window open")
        ticks = 0
        while self.clock.now() < decision.window_end:
            await self.tick()
            ticks += 1
            if ticks % replan_every == 0:
                await self._replan()
            await self.clock.sleep(interval)

        return await self.close_out()

    async def _prearm(self, decision: Decision, now: datetime) -> None:
        """Write the window's opening command at decision time, not at 18:00.

        The command is a scheduler group bounded to the window, so writing it
        early changes nothing before 18:00. What it removes is the gap at 18:00
        between the window opening and a cloud write landing, during which the
        owner's own schedule is in charge.
        """
        mode, reason = self.choose_mode(None, decision.window_start)
        slot = self._slot_for(decision.window_start)
        kw = slot.battery_ac_kw if mode is BatteryMode.FORCE_EXPORT and slot else 0.0
        try:
            await self.controller.apply(ControlCommand(
                timestamp=now, mode=mode, power_kw=kw,
                reason=f"pre-armed for {decision.window_start:%H:%M}: {reason}"))
        except Exception as exc:  # noqa: BLE001 - the first tick at 18:00 writes it anyway
            log.warning("could not pre-arm the window (%s); the first tick will write it", exc)

    async def _replan(self) -> None:
        """Rebuild the plan mid-window from fresh telemetry and forecasts."""
        if self.last_telemetry is None:
            return
        now = self.clock.now()
        solar, load, degraded = await self._curves(now, self.decision.window_end)  # type: ignore[union-attr]
        try:
            fresh = self.engine.plan(
                now=now,
                telemetry=self.last_telemetry,
                solar_kw_at=solar,
                load_kw_at=load,
                degraded=degraded or self.degraded,
                notes=["mid-window replan"],
                already_exported_kwh=self.exported_kwh,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("replan failed, keeping the existing plan: %s", exc)
            return
        # Keep the original window bounds and monitor; only the slot profile changes.
        assert self.decision is not None
        self.decision.slots = fresh.slots
        # Keep the rationale readable: the full replan history goes to the log and
        # the ledger, but the Decision keeps only the most recent few.
        self.decision.rationale = [
            line for line in self.decision.rationale if not line.startswith("replan ")
        ][-8:] + [f"replan {now:%H:%M}: {fresh.summary()}"]
        log.info("replan at %s: %s", now.strftime("%H:%M"), fresh.summary())

    async def close_out(self) -> DailyOutcome:
        """Return the battery to normal and write the day's outcome."""
        now = self.clock.now()
        await self.controller.release(now=now, reason="credit window closed")
        tel = self.last_telemetry
        monitor = self.monitor
        secured = monitor.credit_secured if monitor else False
        verified = monitor.credit_verified if monitor else False
        partial = self.decision is not None and now < self.decision.window_end

        notes = [monitor.report()] if monitor else []
        if partial:
            notes.append(f"PARTIAL: closed out at {now:%H:%M}, before the window ended — "
                         f"a later row for this date supersedes this one")
        uncertain = [b for b in (monitor.breached_hours() if monitor else []) if not b.clearly_breached]
        if uncertain and not any(b.clearly_breached for b in monitor.breached_hours()):
            notes.append(
                "CHECK BILL: " + ", ".join(f"{b.hour_start:%H:%M} estimated {b.imported_kwh * 1000:.0f} Wh"
                                           for b in uncertain)
                + " — over the limit, but within the error of five-minute cloud readings"
            )
        if monitor and monitor.breach_free and not verified:
            notes.append("UNVERIFIED: no breach seen, but not every hour was watched, "
                         "so the credit cannot be claimed")

        pnl = project_daily_pnl(
            self.cfg.plan.tariff,
            in_window_export_kwh=self.exported_kwh,
            peak_import_kwh=monitor.total_import_kwh if monitor else 0.0,
            credit_secured=secured,
        )

        outcome = DailyOutcome(
            date=now.date().isoformat(),
            decision=self.decision,
            exported_kwh=round(self.exported_kwh, 3),
            imported_kwh=round(monitor.total_import_kwh, 4) if monitor else 0.0,
            hourly_import=sorted(monitor.buckets.values(), key=lambda b: b.hour_start) if monitor else [],
            final_soc_pct=round(tel.soc_pct, 2) if tel else 0.0,
            final_energy_kwh=round(tel.battery_energy_kwh, 2) if tel else 0.0,
            credit_secured=secured,
            credit_verified=verified,
            partial=partial,
            super_export_kwh=round(min(self.exported_kwh, self.cfg.plan.super_export_cap_kwh), 3),
            estimated_revenue_aud=round(pnl.net_aud, 3),
            notes=notes,
        )
        if self.ledger:
            self.ledger.record_outcome(outcome)
        self.status_note = "window closed"
        return outcome


class FreeChargeRunner:
    """Fills the battery during the 11:00-14:00 window, where import costs $0.00.

    This is the other half of the arbitrage and it is worth as much as the evening
    control: every kWh put in here is free, and every kWh of capacity left unfilled
    at 14:00 is a kWh that will later be bought at $0.407 (shoulder) or $0.528 (peak),
    or that cannot be sold at $0.10 in the evening.

    The policy is deliberately simple — charge at full power until the pack reaches
    the target SOC or the window closes — because there is no reason to be clever
    when the input price is zero. The one subtlety is the inverter's shared AC port:
    charging from the grid at 10 kW while the array is producing 8 kW would exceed
    the 10 kW rating, so the commanded charge power is reduced by the measured PV.
    """

    def __init__(
        self,
        cfg: AppConfig,
        *,
        telemetry: TelemetryProvider,
        controller: BatteryController,
        clock: Clock,
        ledger: Ledger | None = None,
    ) -> None:
        self.cfg = cfg
        self.telemetry = telemetry
        self.controller = controller
        self.clock = clock
        self.ledger = ledger

    def window_bounds(self, reference: datetime) -> tuple[datetime, datetime]:
        start = reference.replace(
            hour=self.cfg.plan.free_charge_start.hour,
            minute=self.cfg.plan.free_charge_start.minute,
            second=0, microsecond=0,
        )
        end = reference.replace(
            hour=self.cfg.plan.free_charge_end.hour,
            minute=self.cfg.plan.free_charge_end.minute,
            second=0, microsecond=0,
        )
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def charge_power_kw(self, tel: Telemetry) -> float:
        """Max charge rate that keeps total AC throughput inside the inverter limit."""
        target = self.cfg.battery.max_charge_kw
        if self.cfg.inverter.solar_shares_ac_limit:
            target = min(target, max(0.0, self.cfg.inverter.ac_limit_kw - tel.solar_kw))
        return target

    async def run_window(self) -> float:
        """Charge until target SOC or window close. Returns kWh added."""
        if not self.cfg.strategy.charge_in_free_window:
            log.info("free-window charging disabled in config")
            return 0.0

        start, end = self.window_bounds(self.clock.now())
        target_soc = self.cfg.strategy.free_window_target_soc_pct
        interval = self.cfg.strategy.control_interval_seconds
        start_energy: float | None = None

        while self.clock.now() < start:
            await self.clock.sleep(min(interval, (start - self.clock.now()).total_seconds()))

        log.info("free charge window open — target %.0f%% SOC", target_soc)
        while self.clock.now() < end:
            now = self.clock.now()
            try:
                tel = await self.telemetry.read(now)
            except ProviderError as exc:
                log.error("telemetry unavailable during free charge: %s", exc)
                await self.clock.sleep(interval)
                continue

            if start_energy is None:
                start_energy = tel.battery_energy_kwh
            if isinstance(self.controller, SafetyWrapper):
                self.controller.observe_soc(tel.soc_pct)

            if tel.soc_pct >= target_soc:
                await self.controller.release(now=now, reason=f"reached {target_soc:.0f}% SOC")
                log.info("free charge complete at %s (%.1f%%)", now.strftime("%H:%M"), tel.soc_pct)
                break

            kw = self.charge_power_kw(tel)
            await self.controller.apply(
                ControlCommand(
                    timestamp=now, mode=BatteryMode.FORCE_CHARGE, power_kw=-kw,
                    reason=f"free import window, {tel.soc_pct:.0f}% -> {target_soc:.0f}%",
                )
            )
            if self.ledger:
                self.ledger.record_sample(tel, -kw, "free charge")
            await self.clock.sleep(interval)

        final = await self.telemetry.read(self.clock.now())
        await self.controller.release(now=self.clock.now(), reason="free charge window closed")
        added = final.battery_energy_kwh - (start_energy or final.battery_energy_kwh)
        log.info("free charge window added %.2f kWh at $0.00/kWh", added)
        return added
