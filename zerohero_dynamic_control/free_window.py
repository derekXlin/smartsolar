"""Assurance for the free 11:00-14:00 charging window.

This window is not controlled by us — it is a ForceCharge group configured in the
FoxESS app. But it is the single most valuable three hours of the day, so "someone
set it up once" is not good enough. It is worth roughly:

    ~30 kWh of energy at $0.00/kWh that would otherwise be bought at
    $0.407 (shoulder) or $0.528 (peak)  =>  $12-$15 of avoided cost per day,
    plus it is what makes the evening's 15 kWh Super Export run possible at all.

A silently broken window is expensive and invisible: the battery just quietly
arrives at 18:00 half full, the evening plan shrinks, and the bill creeps up.
Failure modes seen in the wild:

    * the group got disabled, or its enable flag was cleared by a firmware update
    * someone edited the times in the app and the window no longer covers 11:00-14:00
    * the group is there and enabled but the battery is already full, so nothing
      happens (fine) — or it is NOT full and still nothing happens (not fine)
    * minSoc/maxSoc caps stop the charge early
    * another controller, or our own crashed run, overwrote the scheduler

So this module checks two different things, because they fail independently:

    1. CONFIGURATION, once before the window opens: does an *enabled* ForceCharge
       group actually cover 11:00-14:00? One API call, and it answers the question
       the owner actually asked — "are we set up to force charge?"

    2. BEHAVIOUR, a handful of times during the window: is the battery really
       drawing power, is SOC climbing, and did it reach target by 14:00?

Config can pass but behaviour fail (a full battery, a grid fault, a BMS limit), and
behaviour can pass but config be wrong (charging from PV alone, not from the free
grid import). Checking only one of them would miss real failures.

API cost: 1 audit call + 18 behaviour polls at the default 10-minute cadence = 19 of
the 1440 daily allowance. Deliberately cheap; the evening control loop needs the rest.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from .clock import Clock
from .config import AppConfig
from .controllers.base import BatteryController
from .data_providers.base import ProviderError, TelemetryProvider
from .ledger import Ledger
from .models import BatteryMode, ControlCommand, FreeWindowOutcome, Telemetry

log = logging.getLogger(__name__)

CHARGING_WORK_MODES = {"ForceCharge"}


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _group_minutes(group: dict, prefix: str, default_hour: int) -> int:
    return int(group.get(f"{prefix}Hour", default_hour) or 0) * 60 + int(
        group.get(f"{prefix}Minute", 0) or 0
    )


def audit_schedule(
    groups: list[dict],
    *,
    window_start: time,
    window_end: time,
    require_coverage: float = 0.9,
) -> tuple[bool, list[str]]:
    """Does an enabled ForceCharge group actually cover the free window?

    Returns (ok, findings). ``require_coverage`` is the fraction of the window that
    must be covered — 0.9 tolerates a group that stops at 13:59 rather than 14:00,
    which is how the FoxESS app writes an inclusive end minute.
    """
    findings: list[str] = []
    want_start, want_end = _minutes(window_start), _minutes(window_end)
    want_span = max(1, want_end - want_start)

    charge_groups = [
        g for g in groups
        if str(g.get("workMode")) in CHARGING_WORK_MODES
    ]
    if not charge_groups:
        modes = sorted({str(g.get("workMode")) for g in groups}) or ["(no groups at all)"]
        findings.append(
            f"NO ForceCharge group in the FoxESS scheduler — found {', '.join(modes)}. "
            f"The free {window_start:%H:%M}-{window_end:%H:%M} window will not charge the battery."
        )
        return False, findings

    # Union the covered minutes across every enabled charge group.
    covered: set[int] = set()
    disabled_overlap = False
    for g in charge_groups:
        gs = _group_minutes(g, "start", 0)
        ge = _group_minutes(g, "end", 23) + 1  # FoxESS end minute is inclusive
        overlaps = gs < want_end and ge > want_start
        if not int(g.get("enable", 1) or 0):
            if overlaps:
                disabled_overlap = True
            continue
        covered.update(range(max(gs, want_start), min(ge, want_end)))

    if disabled_overlap and not covered:
        findings.append(
            "a ForceCharge group covers the window but its enable flag is 0 — "
            "switch it back on in the FoxESS app"
        )
        return False, findings

    fraction = len(covered) / want_span
    if fraction >= require_coverage:
        findings.append(
            f"ForceCharge covers {fraction * 100:.0f}% of "
            f"{window_start:%H:%M}-{window_end:%H:%M}"
        )
        return True, findings

    if not covered:
        spans = ", ".join(
            f"{_group_minutes(g, 'start', 0) // 60:02d}:{_group_minutes(g, 'start', 0) % 60:02d}"
            f"-{_group_minutes(g, 'end', 23) // 60:02d}:{_group_minutes(g, 'end', 23) % 60:02d}"
            for g in charge_groups
        )
        findings.append(
            f"a ForceCharge group exists but covers {spans}, which does not overlap the "
            f"free {window_start:%H:%M}-{window_end:%H:%M} window at all"
        )
    else:
        missing = want_span - len(covered)
        findings.append(
            f"ForceCharge covers only {fraction * 100:.0f}% of the free window — "
            f"{missing} minutes of $0.00 energy are unreachable"
        )
    return False, findings


class FreeChargeAssurance:
    """Verifies the free window without taking control of it."""

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
        self.last_outcome: FreeWindowOutcome | None = None

    # ---------------------------------------------------------------- windows
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

    # ------------------------------------------------------------------ audit
    async def audit(self) -> tuple[bool, list[str]]:
        """Read the live scheduler and confirm it is configured to force charge."""
        getter = getattr(self.controller, "read_schedule", None)
        if getter is None:
            return True, ["controller cannot read its schedule; skipping the config audit"]
        try:
            groups = await getter()
        except Exception as exc:  # noqa: BLE001
            log.warning("free-window schedule audit could not read the scheduler: %s", exc)
            return True, [f"schedule unreadable ({exc}); relying on the behaviour checks"]

        ok, findings = audit_schedule(
            groups,
            window_start=self.cfg.plan.free_charge_start,
            window_end=self.cfg.plan.free_charge_end,
        )
        for line in findings:
            (log.info if ok else log.error)("free-window audit: %s", line)
        return ok, findings

    # -------------------------------------------------------------- behaviour
    def _evaluate_sample(self, tel: Telemetry, target_soc: float) -> tuple[bool, str]:
        """Is this sample consistent with 'the free window is working'?"""
        a = self.cfg.strategy.free_window_assurance
        if tel.soc_pct >= target_soc - 0.5:
            return True, f"already at {tel.soc_pct:.0f}% SOC — nothing left to charge"
        charging_kw = max(0.0, -tel.battery_kw)
        if charging_kw >= a.min_charge_power_kw:
            # Distinguish free grid energy from ordinary solar self-consumption.
            if tel.import_kw < a.min_charge_power_kw and tel.solar_kw > charging_kw:
                return True, (
                    f"charging {charging_kw:.1f} kW from PV; no grid import to exploit "
                    f"(solar {tel.solar_kw:.1f} kW covers it)"
                )
            return True, f"charging {charging_kw:.1f} kW, importing {tel.import_kw:.1f} kW at $0.00"
        return False, (
            f"NOT charging ({charging_kw:.2f} kW) at {tel.soc_pct:.0f}% SOC with "
            f"{target_soc - tel.soc_pct:.0f}% of free headroom going begging"
        )

    async def _remediate(self, now: datetime, tel: Telemetry) -> bool:
        """Optionally push a ForceCharge command ourselves."""
        if not self.cfg.strategy.free_window_assurance.remediate:
            return False
        kw = self.cfg.battery.max_charge_kw
        if self.cfg.inverter.solar_shares_ac_limit:
            kw = min(kw, max(0.0, self.cfg.inverter.ac_limit_kw - tel.solar_kw))
        log.warning("free-window remediation: commanding ForceCharge at %.1f kW", kw)
        try:
            await self.controller.apply(
                ControlCommand(
                    timestamp=now, mode=BatteryMode.FORCE_CHARGE, power_kw=-kw,
                    reason="free-window assurance: battery was not charging",
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("free-window remediation failed: %s", exc)
            return False

    # -------------------------------------------------------------------- run
    async def run_window(self) -> FreeWindowOutcome:
        a = self.cfg.strategy.free_window_assurance
        start, end = self.window_bounds(self.clock.now())
        target = a.target_soc_pct

        outcome = FreeWindowOutcome(date=start.date().isoformat(), target_soc_pct=target)

        schedule_ok, findings = await self.audit()
        outcome.schedule_ok = schedule_ok
        outcome.findings.extend(findings)

        while self.clock.now() < start:
            await self.clock.sleep(min(60, max(1, (start - self.clock.now()).total_seconds())))

        first: Telemetry | None = None
        last: Telemetry | None = None
        consecutive_failures = 0
        while self.clock.now() < end:
            now = self.clock.now()
            try:
                tel = await self.telemetry.read(now)
            except ProviderError as exc:
                # Log, do not just record. A silent watcher is worse than none:
                # it looks healthy right up until the 14:00 summary, by which
                # point the free window has closed and nothing can be done. A
                # revoked API key, an expired quota or a NAS clock drift all
                # land here and all need to be visible immediately.
                consecutive_failures += 1
                log.error("free-window check %s: telemetry unavailable (%d in a row): %s",
                          now.strftime("%H:%M"), consecutive_failures, exc)
                outcome.findings.append(f"{now:%H:%M} telemetry unavailable: {exc}")
                if consecutive_failures == 2:
                    log.error("free-window assurance is BLIND — cannot tell whether the "
                              "battery is charging. Check the API key, the daily call "
                              "quota and the system clock.")
                await self.clock.sleep(a.check_interval_seconds)
                continue
            consecutive_failures = 0

            first = first or tel
            last = tel

            if (now - start).total_seconds() < a.startup_grace_minutes * 60:
                ok, note = self._evaluate_sample(tel, target)
                if not ok:
                    log.info("free-window check %s: %s (inside the %d-minute startup "
                             "grace, not counted)", now.strftime("%H:%M"), note,
                             a.startup_grace_minutes)
                await self.clock.sleep(a.check_interval_seconds)
                continue

            outcome.samples += 1
            ok, note = self._evaluate_sample(tel, target)
            if ok:
                outcome.charging_samples += 1
            else:
                outcome.failed_samples += 1
                log.warning("free-window check %s: %s", now.strftime("%H:%M"), note)
                if await self._remediate(now, tel):
                    outcome.remediated = True
            if outcome.samples == 1 or not ok:
                outcome.findings.append(f"{now:%H:%M} {note}")
            if self.ledger:
                self.ledger.record_sample(tel, -max(0.0, -tel.battery_kw), "free-window check")

            await self.clock.sleep(a.check_interval_seconds)

        # ---- outcome at 14:00 ------------------------------------------------
        try:
            final = await self.telemetry.read(self.clock.now())
        except ProviderError:
            final = last
        if first and final:
            outcome.start_soc_pct = round(first.soc_pct, 1)
            outcome.final_soc_pct = round(final.soc_pct, 1)
            outcome.energy_added_kwh = round(
                max(0.0, final.battery_energy_kwh - first.battery_energy_kwh), 2
            )

        self._score(outcome)
        if self.ledger:
            self.ledger.record_free_window(outcome)
        self.last_outcome = outcome
        return outcome

    def _achievable_soc_pct(self, start_soc_pct: float) -> float:
        """The highest SOC three hours of charging can physically reach.

        Charge power is finite, so a window that starts at 17% cannot end at 100%
        no matter how perfectly it is configured:

            reachable = start + (max_charge_kw x hours x efficiency) / capacity

        Scoring against a flat threshold instead would raise a false alarm on every
        day that begins with a depleted battery — which is exactly the day the owner
        most needs to trust the alert.
        """
        cfg = self.cfg
        start, end = self.window_bounds(self.clock.now())
        hours = (end - start).total_seconds() / 3600.0
        gain_kwh = cfg.battery.max_charge_kw * hours * cfg.battery.charge_efficiency
        gain_pct = gain_kwh / cfg.battery.usable_capacity_kwh * 100.0
        return min(cfg.strategy.free_window_assurance.target_soc_pct, start_soc_pct + gain_pct)

    def _score(self, outcome: FreeWindowOutcome) -> None:
        """Turn the window into a verdict and a dollar figure."""
        cfg = self.cfg
        a = cfg.strategy.free_window_assurance

        outcome.achievable_soc_pct = round(self._achievable_soc_pct(outcome.start_soc_pct), 1)
        # Judge against whichever bar is lower: the owner's threshold, or physics.
        bar = min(a.alert_soc_pct, outcome.achievable_soc_pct - a.achievable_tolerance_pct)

        shortfall_pct = max(0.0, outcome.achievable_soc_pct - outcome.final_soc_pct)
        unclaimed = shortfall_pct / 100.0 * cfg.battery.usable_capacity_kwh
        outcome.unclaimed_free_kwh = round(unclaimed, 2)

        # Energy not taken for free will be bought later at the overnight blend.
        rate = cfg.plan.tariff.blended_import_rate(cfg.plan.credit_window_end, 14.0)
        outcome.missed_value_aud = round(unclaimed * rate, 2)

        healthy = (
            outcome.schedule_ok
            and outcome.failed_samples == 0
            and outcome.samples > 0          # a window we never observed is not a pass
            and outcome.final_soc_pct >= bar
        )
        if outcome.samples == 0:
            outcome.findings.append(
                "no telemetry was read during the entire window — this verdict means "
                "'could not check', not 'all good'"
            )
        outcome.ok = healthy

        if outcome.achievable_soc_pct < a.alert_soc_pct - a.achievable_tolerance_pct:
            outcome.findings.append(
                f"charge-rate limited: starting at {outcome.start_soc_pct:.0f}% SOC, three "
                f"hours at {cfg.battery.max_charge_kw:.0f} kW can only reach "
                f"{outcome.achievable_soc_pct:.0f}% — judging against that, not "
                f"{a.alert_soc_pct:.0f}%"
            )
        if outcome.final_soc_pct < bar:
            outcome.findings.append(
                f"finished at {outcome.final_soc_pct:.0f}% SOC against a reachable "
                f"{outcome.achievable_soc_pct:.0f}% — {unclaimed:.1f} kWh of free energy "
                f"left unclaimed, worth ${outcome.missed_value_aud:.2f} at the "
                f"${rate:.3f}/kWh you will pay for it instead"
            )
        if outcome.samples and outcome.failed_samples:
            outcome.findings.append(
                f"{outcome.failed_samples} of {outcome.samples} checks found the battery "
                f"idle while free energy was available"
            )

        level = log.info if healthy else log.error
        level(
            "free window %s: %s | %.0f%% -> %.0f%% (+%.1f kWh) | %d/%d checks charging",
            outcome.date,
            "OK" if healthy else "PROBLEM",
            outcome.start_soc_pct, outcome.final_soc_pct, outcome.energy_added_kwh,
            outcome.charging_samples, outcome.samples,
        )
        for line in outcome.findings:
            level("  %s", line)
