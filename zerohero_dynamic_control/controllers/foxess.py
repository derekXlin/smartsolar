"""FoxESS battery controller, driven through the Cloud scheduler.

WHY THE SCHEDULER AND NOT A WORK-MODE SETTING
---------------------------------------------
FoxESS exposes a plain ``WorkMode`` device setting, but it has no power argument. The
scheduler is the only surface that accepts a true discharge power, and it gives us two
things that matter here:

    fdPwr   force-discharge power, in WATTS   -> our kW setpoint
    fdSoc   force-discharge stop SOC, in %    -> a HARDWARE-ENFORCED floor

fdSoc is the important one. It means the inverter itself stops discharging at our
emergency floor. If this process crashes at 19:30, or the Wi-Fi drops, or the cloud API
goes down mid-window, the battery still will not run itself flat — the floor lives in
the inverter, not in our control loop. We set it on every command for exactly that
reason, and it is a strictly better guarantee than software can offer.

BASELINE PRESERVATION
---------------------
``scheduler/enable`` replaces the WHOLE group list; there is no per-group patch. So a
naive write would wipe whatever the owner configured in the FoxESS app.

This site already has a force-charge group covering the free 11:00-14:00 window, and
losing it would be expensive: the house would miss a whole day of $0.00 energy and buy
it back later at $0.407-$0.528. Restoring the baseline at 21:00 is not good enough on
its own, because a failed restore (crash, network drop, quota) would leave the group
missing until someone noticed.

So this controller MERGES instead of replacing: it keeps every existing group that does
not overlap our credit window and appends its own. The owner's 11:00-14:00 force charge
therefore survives even if close-out never runs at all. Restore is still performed at
close-out, but it is now a tidy-up rather than the only thing standing between a bug and
a lost charging window.

CALL BUDGET
-----------
Each setpoint change is one write against the 1440/day allowance, so the loop's
command deadband is doing real work here — see ``ControllerCapabilities`` below, which
reports a 2-second minimum interval to match the documented update limit.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from ..foxess_client import FoxESSClient, FoxESSError, FoxESSQuotaExhausted
from ..models import BatteryMode
from .base import BatteryController, ControllerCapabilities, ControllerError

log = logging.getLogger(__name__)

MAX_SCHEDULER_GROUPS = 8
"""Conservative default. The live H3-10.0-Smart reports maxGroupCount=96, so this
is read from the inverter when available and only used as a floor."""

CATCH_ALL_MINUTES = 20 * 60
"""A group spanning at least this long is treated as an all-day default."""

MODE_MAP: dict[BatteryMode, str] = {
    BatteryMode.SELF_CONSUMPTION: "SelfUse",
    BatteryMode.FORCE_EXPORT: "ForceDischarge",
    BatteryMode.FORCE_CHARGE: "ForceCharge",
    BatteryMode.BACKUP: "Backup",
    BatteryMode.HOLD: "SelfUse",
}


class FoxESSController(BatteryController):
    name = "foxess"

    def __init__(
        self,
        client: FoxESSClient,
        serial_number: str,
        *,
        window_start: time,
        window_end: time,
        min_soc_on_grid_pct: int,
        max_power_kw: float,
        fd_soc_pct: int | None = None,
        preserve_baseline: bool = True,
        baseline_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.sn = serial_number
        self.window_start = window_start
        self.window_end = window_end
        self.min_soc_on_grid_pct = int(min_soc_on_grid_pct)
        # fdSoc is the DEADMAN floor, and it is deliberately higher than the
        # absolute hardware minimum. If this process dies at 19:30 the inverter
        # keeps force-discharging at the last power it was given until the group
        # expires, so whatever fdSoc says is where an unattended battery stops.
        # Parking it at the emergency floor would let a crashed container empty
        # the pack to 10%; parking it at the planning reserve caps the damage at
        # a level the house can still run on overnight. The live loop lowers it
        # only when the engine decides the credit genuinely needs the extra depth.
        self.fd_soc_pct = int(fd_soc_pct if fd_soc_pct is not None else min_soc_on_grid_pct)
        self.max_power_kw = max_power_kw
        self.preserve_baseline = preserve_baseline
        # Persist the owner's schedule to disk the moment we first read it.
        # Once we have written our own group, the inverter no longer remembers
        # what was there before, so a restart mid-window would adopt OUR group
        # as the baseline and restore it at 21:00 — permanently losing theirs.
        self.baseline_path = baseline_path

        self._baseline: list[dict[str, Any]] | None = None
        self.max_groups = MAX_SCHEDULER_GROUPS
        self.max_soc_pct = 100
        self._baseline_loaded = False
        self._mode: BatteryMode = BatteryMode.SELF_CONSUMPTION
        self._power_kw: float = 0.0

    def capabilities(self) -> ControllerCapabilities:
        return ControllerCapabilities(
            supports_power_setpoint=True,   # fdPwr, in watts
            supports_soc_target=True,       # fdSoc
            min_command_interval_seconds=2.0,  # documented update limit
            power_resolution_kw=0.001,      # fdPwr is an integer number of watts
            max_power_kw=self.max_power_kw,
        )

    # ------------------------------------------------------------- baseline
    def _is_ours(self, group: dict[str, Any]) -> bool:
        """A group we wrote on an earlier run, not the owner's."""
        return (
            group.get("workMode") == "ForceDischarge"
            and self._minutes(group, "start", 0) == self.window_start.hour * 60 + self.window_start.minute
            and self._minutes(group, "end", 23) == self._end_hour() * 60 + self._group_end_minute()
        )

    def _read_persisted_baseline(self) -> list[dict[str, Any]] | None:
        if self.baseline_path is None or not self.baseline_path.exists():
            return None
        try:
            saved = json.loads(self.baseline_path.read_text())
            if saved.get("date") != date.today().isoformat():
                return None
            log.info("recovered the owner's schedule from %s (%d groups) — the "
                     "inverter no longer holds it", self.baseline_path, len(saved["groups"]))
            return saved["groups"]
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the persisted baseline: %s", exc)
            return None

    def _persist_baseline(self, groups: list[dict[str, Any]]) -> None:
        if self.baseline_path is None:
            return
        try:
            self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
            self.baseline_path.write_text(json.dumps(
                {"date": date.today().isoformat(), "groups": groups}, indent=2))
        except OSError as exc:
            log.error("could not persist the owner's schedule (%s); a restart "
                      "before close-out would lose it", exc)

    async def _load_baseline(self) -> None:
        if self._baseline_loaded or not self.preserve_baseline:
            self._baseline_loaded = True
            return
        recovered = self._read_persisted_baseline()
        if recovered is not None:
            self._baseline = recovered
            self._baseline_loaded = True
            return
        try:
            current = await self.client.scheduler_get(self.sn)
            # The inverter pads the list with blank disabled placeholders; keep
            # only groups that actually do something so our insert stays readable.
            real = [
                g for g in (current.get("groups") or [])
                if g.get("workMode") and int(g.get("enable", 1) or 0)
            ]
            # Drop the all-day catch-all. The API reports a 00:00-23:59 SelfUse
            # group that does NOT appear in the FoxESS app, so it is an implicit
            # default rather than something the owner configured. Writing it back
            # alongside any other group is rejected with errno 42023 "Time
            # overlap" — which is what stopped the controller taking over on its
            # first live evening.
            # Exclude our own group from a previous run in this window, else we
            # would "restore" it over the top of the owner's real schedule.
            self._baseline = [
                g for g in real if not self._is_catch_all(g) and not self._is_ours(g)
            ]
            self._persist_baseline(self._baseline)
            dropped = len(real) - len(self._baseline)
            if dropped:
                log.info("ignoring %d all-day default group(s); FoxESS rejects "
                         "writes that overlap them", dropped)
            self.max_groups = max(MAX_SCHEDULER_GROUPS, int(current.get("maxGroupCount") or 0))
            log.info("saved the existing FoxESS schedule (%d groups) for restore at close-out",
                     len(self._baseline))
        except FoxESSError as exc:
            # Not fatal, but the owner must know we cannot put things back.
            log.warning("could not read the existing FoxESS schedule (%s); "
                        "close-out will disable the scheduler rather than restore it", exc)
            self._baseline = None
        self._baseline_loaded = True

    # -------------------------------------------------------------- groups
    @staticmethod
    def _minutes(group: dict[str, Any], prefix: str, default: int) -> int:
        return int(group.get(f"{prefix}Hour", default) or 0) * 60 + int(
            group.get(f"{prefix}Minute", 0) or 0
        )

    def _overlaps_our_window(self, group: dict[str, Any]) -> bool:
        start = self._minutes(group, "start", 0)
        end = self._minutes(group, "end", 23)
        ours_start = self.window_start.hour * 60 + self.window_start.minute
        ours_end = self.window_end.hour * 60 + self.window_end.minute
        return start < ours_end and ours_start <= end

    def _is_catch_all(self, group: dict[str, Any]) -> bool:
        """A group spanning most of the day, e.g. the 00:00-23:59 SelfUse default.

        These overlap our window by construction but must NEVER be removed: the
        real inverter uses one to govern every hour we do not control. Dropping it
        would leave 21:00-11:00 and 14:00-18:00 with no rule at all.
        """
        span = self._minutes(group, "end", 23) - self._minutes(group, "start", 0)
        return span >= CATCH_ALL_MINUTES

    def _merged_groups(self, ours: dict[str, Any]) -> list[dict[str, Any]]:
        """Insert our group into the owner's list without disturbing the rest.

        Ordering is load-bearing. The observed inverter config lists specific
        groups first and an all-day SelfUse catch-all last, and the specific ones
        demonstrably win — so this is a first-match-wins list and our group has to
        sit AHEAD of the catch-all to take effect.

        Strategy, in priority order:
          1. If a non-catch-all group already covers our window (the owner's own
             18:00-19:05 ForceDischarge), replace it IN PLACE. That preserves the
             list order exactly, whatever the inverter's precedence rule turns out
             to be, and is the least surprising edit.
          2. Otherwise insert ours immediately before the first catch-all.
          3. Otherwise append.

        Nothing is ever deleted, so the 11:00-14:00 ForceCharge survives even if
        close-out never runs.
        """
        baseline = list(self._baseline or [])
        if not baseline:
            return [ours]

        for i, g in enumerate(baseline):
            if self._overlaps_our_window(g) and not self._is_catch_all(g):
                log.info("replacing the existing %s group at %02d:%02d in place with our "
                         "%s-%s window", g.get("workMode"), self._minutes(g, "start", 0) // 60,
                         self._minutes(g, "start", 0) % 60,
                         self.window_start.strftime("%H:%M"), self.window_end.strftime("%H:%M"))
                merged = list(baseline)
                merged[i] = ours
                return self._cap(merged, ours)

        return self._cap([*baseline, ours], ours)

    def _cap(self, groups: list[dict[str, Any]], ours: dict[str, Any]) -> list[dict[str, Any]]:
        if len(groups) <= self.max_groups:
            return groups
        log.warning("inverter accepts at most %d scheduler groups; trimming %d",
                    self.max_groups, len(groups) - self.max_groups)
        kept = [g for g in groups if g is not ours][: self.max_groups - 1]
        return [ours, *kept]

    def _group(self, work_mode: str, power_kw: float, fd_soc: int) -> dict[str, Any]:
        """One scheduler group covering our control window.

        SELF-TERMINATING BY CONSTRUCTION. The group always carries an explicit
        start AND end inside the credit window, so the inverter itself ends the
        forced mode at 20:59 and falls back to the all-day SelfUse group. Nothing
        about that depends on this process still being alive: a killed container,
        a crashed NAS, a severed network or an expired API quota all end the same
        way, with the battery back under its normal rules at 21:00.

        That is why the controller never writes an open-ended or all-day forced
        group, and why ``_assert_bounded`` below refuses to let it.
        """
        return {
            "enable": 1,
            "startHour": self.window_start.hour,
            "startMinute": self.window_start.minute,
            "endHour": self.window_end.hour,
            # The window is exclusive of its end instant; FoxESS groups are inclusive
            # of the end minute, so stop one minute short to avoid overrunning 21:00.
            "endMinute": self._group_end_minute(),
            "workMode": work_mode,
            "minSocOnGrid": self.min_soc_on_grid_pct,
            # maxSoc is not optional. Every group the inverter itself returns
            # carries it, and omitting it is rejected with errno 40257
            # "Parameters do not meet expectations" — an error that names
            # nothing, so the only way to find it is to diff our payload against
            # a group the device wrote itself.
            "maxSoc": int(self.max_soc_pct),
            "fdSoc": int(fd_soc),
            # fdPwr is an integer number of WATTS.
            "fdPwr": int(round(max(0.0, min(self.max_power_kw, abs(power_kw))) * 1000)),
        }

    def _assert_bounded(self, group: dict[str, Any]) -> None:
        """Refuse to write a forced group that could outlive the control window.

        This is a last-line check against a future edit quietly removing the end
        time. An unbounded ForceDischarge group is the single worst thing this
        program could write to the inverter: it would export the battery flat
        overnight at $0.02/kWh and then buy it back at $0.407 in the morning,
        every day, until someone noticed.
        """
        if group.get("workMode") not in ("ForceDischarge", "ForceCharge"):
            return
        start = self._minutes(group, "start", 0)
        end = self._minutes(group, "end", 23)
        limit = self.window_end.hour * 60 + self.window_end.minute
        if end >= limit or end <= start:
            raise ControllerError(
                f"refusing to write an unbounded {group['workMode']} group "
                f"({start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}); "
                f"it must end before {self.window_end:%H:%M} so the inverter "
                f"releases the battery without us"
            )

    def _group_end_minute(self) -> int:
        return max(0, self.window_end.minute - 1) if self.window_end.minute else 59

    def _end_hour(self) -> int:
        return self.window_end.hour if self.window_end.minute else max(0, self.window_end.hour - 1)

    async def _push(self, mode: BatteryMode, power_kw: float, *, critical: bool = False) -> None:
        await self._load_baseline()
        work_mode = MODE_MAP.get(mode, "SelfUse")
        group = self._group(work_mode, power_kw, self.fd_soc_pct)
        group["endHour"] = self._end_hour()
        self._assert_bounded(group)
        groups = self._merged_groups(group)
        try:
            await self.client.scheduler_enable(self.sn, groups, critical=critical)
        except FoxESSQuotaExhausted as exc:
            # Do not raise: a skipped setpoint nudge is far better than aborting the
            # window. The existing group stays in force and the loop retries later.
            log.warning("skipping FoxESS write, daily budget guard: %s", exc)
            return
        except FoxESSError as exc:
            if "42023" in str(exc):
                raise ControllerError(
                    f"FoxESS rejected the schedule as overlapping (errno 42023). "
                    f"Groups written: "
                    f"{[(g['startHour'], g['endHour'], g['workMode']) for g in groups]}. "
                    f"FoxESS allows no two groups to share any minute, including "
                    f"its own all-day default."
                ) from exc
            raise ControllerError(f"FoxESS scheduler write failed: {exc}") from exc
        self._mode, self._power_kw = mode, power_kw

    # ------------------------------------------------------------- interface
    async def set_mode(self, mode: BatteryMode, *, now: datetime, reason: str = "") -> None:
        log.info("FoxESS mode -> %s (%s)", MODE_MAP.get(mode, "SelfUse"), reason)
        if mode in (BatteryMode.SELF_CONSUMPTION, BatteryMode.HOLD):
            await self.restore(now=now, reason=reason)
            self._mode = mode
            return
        await self._push(mode, self._power_kw)

    async def set_power(self, power_kw: float, *, now: datetime, reason: str = "") -> None:
        if self._mode in (BatteryMode.SELF_CONSUMPTION, BatteryMode.HOLD):
            return  # nothing to command; the inverter is running its own schedule
        log.info("FoxESS fdPwr -> %d W (%s)", int(round(abs(power_kw) * 1000)), reason)
        await self._push(self._mode, power_kw)

    async def set_soc_target(self, soc_pct: float, *, now: datetime, reason: str = "") -> None:
        """Move the deadman stop-SOC, clamped to the absolute hardware minimum."""
        target = max(self.min_soc_on_grid_pct, int(round(soc_pct)))
        if target != self.fd_soc_pct:
            log.info("FoxESS fdSoc -> %d%% (%s)", target, reason)
            self.fd_soc_pct = target
            await self._push(self._mode, self._power_kw, critical=True)

    async def restore(self, *, now: datetime, reason: str = "") -> None:
        """Put the inverter back the way we found it. Always allowed to spend budget."""
        # Load the baseline first. After a mid-window restart, close-out can be
        # the FIRST thing this controller does, so nothing has populated it yet —
        # and an empty baseline means we disable the scheduler instead of
        # restoring, wiping the owner's groups for the sake of tidying up.
        await self._load_baseline()
        try:
            if self._baseline:
                await self.client.scheduler_enable(self.sn, self._baseline, critical=True)
                log.info("restored the owner's original FoxESS schedule (%s)", reason)
                if self.baseline_path is not None and self.baseline_path.exists():
                    self.baseline_path.unlink()
            else:
                await self.client.scheduler_disable(self.sn, critical=True)
                log.info("disabled the FoxESS scheduler, returning to SelfUse (%s)", reason)
        except FoxESSError as exc:
            # Loud, because leaving the inverter in ForceDischarge overnight would
            # drain the pack into an $0.02 FiT and then import at $0.407 in the morning.
            log.error("FAILED to restore the FoxESS schedule (%s). "
                      "Check the FoxESS app and disable the scheduler manually.", exc)

    async def read_schedule(self) -> list[dict[str, Any]]:
        """Live scheduler groups. Used by the free-window configuration audit."""
        current = await self.client.scheduler_get(self.sn)
        return current.get("groups") or []

    async def health_check(self) -> bool:
        try:
            devices = await self.client.device_list()
            found = any(
                str(d.get("deviceSN") or d.get("sn")) == self.sn for d in devices
            )
            if not found:
                log.error("inverter %s is not in this FoxESS account's device list", self.sn)
            remaining = self.client.budget.remaining(datetime.now())
            log.info("FoxESS health ok; %d of %d daily API calls remaining",
                     remaining, self.client.budget.daily_limit)
            return found
        except FoxESSError as exc:
            log.error("FoxESS health check failed: %s", exc)
            return False

    async def aclose(self) -> None:
        await self.client.aclose()
