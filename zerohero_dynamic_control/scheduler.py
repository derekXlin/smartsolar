"""APScheduler wiring: fire the decision at 17:50 and charge from 11:00.

Two jobs per day, both in Australia/Sydney local time so they track the AEST/AEDT
changeover automatically — which matters here, because the credit window is defined
in local time while sunset moves by two hours across the year.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import timedelta

from . import __version__
from .clock import RealClock
from .config import AppConfig
from .controllers import build_controller
from .data_providers import CachingTelemetryProvider, build_forecast_provider
from .data_providers.base import TelemetryProvider
from .decision_engine import DecisionEngine
from .free_window import FreeChargeAssurance
from .ledger import Ledger
from .runtime import EveningRunner, FreeChargeRunner

log = logging.getLogger(__name__)


class ZeroHeroScheduler:
    def __init__(self, cfg: AppConfig, telemetry: TelemetryProvider) -> None:
        self.cfg = cfg
        self.clock = RealClock(cfg.site.tz)
        # The fastest the pack can physically move, used to spot bad cloud data.
        soc_rate = (
            max(cfg.battery.max_charge_kw, cfg.battery.max_discharge_kw)
            / cfg.battery.usable_capacity_kwh * 100.0 / 60.0
        )
        self.telemetry = CachingTelemetryProvider(
            telemetry, max_soc_rate_pct_per_min=soc_rate
        )
        self.forecast = build_forecast_provider(cfg)
        self.controller = build_controller(cfg)
        self.ledger = Ledger(
            cfg.logging.ledger_path, cfg.logging.decision_log_path, cfg.logging.samples_path
        )
        self.current_runner: EveningRunner | None = None
        self.assurance: FreeChargeAssurance | None = None
        self._stop: asyncio.Event | None = None
        self._scheduler = None

    # ------------------------------------------------------------------- jobs
    async def evening_job(self) -> None:
        log.info("evening job starting")
        runner = EveningRunner(
            self.cfg,
            telemetry=self.telemetry,
            forecast=self.forecast,
            controller=self.controller,
            ledger=self.ledger,
            clock=self.clock,
        )
        self.current_runner = runner
        try:
            if not await self.controller.health_check():
                log.error("controller health check failed — running in degraded mode")
                runner.degraded = True
            await runner.make_decision()
            # Retry the first command. The very first live evening died because
            # one rejected API call raised straight out of the job, and nothing
            # retried it for the remaining three hours of the window.
            for attempt in range(1, 4):
                try:
                    outcome = await runner.run_window()
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    log.exception("control loop failed (attempt %d/3); retrying in 30 s", attempt)
                    await asyncio.sleep(30)
            log.info("evening complete: credit_secured=%s exported=%.2f kWh",
                     outcome.credit_secured, outcome.exported_kwh)
        except Exception:
            log.exception("evening job failed — attempting to restore self-consumption")
            try:
                await runner.close_out()
            except Exception:
                log.exception("close-out also failed; the inverter may be left in force-export")
        finally:
            self.current_runner = None

    async def free_charge_job(self) -> None:
        """Drive the free window ourselves. Only used when we own it."""
        log.info("free charge job starting")
        runner = FreeChargeRunner(
            self.cfg,
            telemetry=self.telemetry,
            controller=self.controller,
            clock=self.clock,
            ledger=self.ledger,
        )
        try:
            await runner.run_window()
        except Exception:
            log.exception("free charge job failed")

    async def free_window_assurance_job(self) -> None:
        """Verify the FoxESS app's ForceCharge group is configured and working."""
        log.info("free-window assurance starting")
        self.assurance = FreeChargeAssurance(
            self.cfg,
            telemetry=self.telemetry,
            controller=self.controller,
            clock=self.clock,
            ledger=self.ledger,
        )
        try:
            await self.assurance.run_window()
        except Exception:
            log.exception("free-window assurance failed")

    # ------------------------------------------------------------------- run
    async def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        sched = AsyncIOScheduler(timezone=self.cfg.site.tz)
        self._scheduler = sched

        lead = timedelta(minutes=self.cfg.strategy.decision_lead_minutes)
        decision_at = (
            self.cfg.plan.credit_window_start.hour * 60
            + self.cfg.plan.credit_window_start.minute
            - int(lead.total_seconds() // 60)
        )
        sched.add_job(
            self.evening_job,
            CronTrigger(hour=decision_at // 60, minute=decision_at % 60, timezone=self.cfg.site.tz),
            id="evening", max_instances=1, misfire_grace_time=600,
            # misfire_grace_time matters: if the host was asleep or the process
            # restarted at 17:55, we still want the job to fire and catch what is
            # left of the window rather than skip the day entirely.
        )
        log.info("scheduled evening job at %02d:%02d %s",
                 decision_at // 60, decision_at % 60, self.cfg.site.timezone)

        assurance = self.cfg.strategy.free_window_assurance
        if self.cfg.strategy.charge_in_free_window:
            sched.add_job(
                self.free_charge_job,
                CronTrigger(
                    hour=self.cfg.plan.free_charge_start.hour,
                    minute=self.cfg.plan.free_charge_start.minute,
                    timezone=self.cfg.site.tz,
                ),
                id="free_charge", max_instances=1, misfire_grace_time=1800,
            )
            log.info("scheduled free charge job at %s", self.cfg.plan.free_charge_start)
        elif assurance.enabled:
            # Start early enough to audit the scheduler BEFORE the window opens,
            # so a disabled ForceCharge group is caught while there is still time
            # to do something about it.
            audit_at = (
                self.cfg.plan.free_charge_start.hour * 60
                + self.cfg.plan.free_charge_start.minute
                - assurance.audit_lead_minutes
            ) % (24 * 60)
            sched.add_job(
                self.free_window_assurance_job,
                CronTrigger(hour=audit_at // 60, minute=audit_at % 60, timezone=self.cfg.site.tz),
                id="free_window_assurance", max_instances=1, misfire_grace_time=1800,
            )
            log.info("scheduled free-window assurance at %02d:%02d (audit) through %s",
                     audit_at // 60, audit_at % 60, self.cfg.plan.free_charge_end)

        self._stop = asyncio.Event()
        sched.start()

        if self.cfg.strategy.catch_up_on_start:
            now = self.clock.now()
            start, end = DecisionEngine(self.cfg).window_bounds(now)
            if start <= now < end:
                mins = int((end - now).total_seconds() // 60)
                log.warning("started inside the credit window with %d min left — "
                            "catching up rather than forfeiting the evening", mins)
                asyncio.create_task(self.evening_job())
        self._install_signal_handlers()
        log.info("zerohero %s (build %s) — local time %s (%s)",
                 __version__, os.environ.get("ZEROHERO_BUILD", "unknown"),
                 self.clock.now().isoformat(), self.cfg.site.timezone)
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            pass
        finally:
            log.info("shutting down")
            sched.shutdown(wait=False)
            await self.shutdown()

    def _install_signal_handlers(self) -> None:
        """Turn SIGTERM/SIGINT into a clean shutdown rather than a hard kill.

        This matters far more in a container than on a desktop. `docker stop`, a
        compose restart, a NAS package update or an image pull all deliver SIGTERM.
        If one arrives at 19:30 the inverter is sitting in ForceDischarge, and
        without this handler it STAYS there: the battery empties overnight into a
        $0.02/kWh feed-in tariff and the house buys it back at $0.407 in the
        morning. Docker's default grace period is 10 seconds, which is ample for
        the one or two API calls a restore needs.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_stop, sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                log.debug("signal %s not installable on this platform", sig)

    def _request_stop(self, sig: signal.Signals) -> None:
        log.warning("received %s — restoring the inverter before exit", sig.name)
        if self._stop is not None:
            self._stop.set()

    async def shutdown(self) -> None:
        """Put the battery back to normal, then close every client.

        Best-effort and heavily guarded: a failure in any one step must not stop
        the others, because the important thing is that the inverter does not stay
        in a forced mode.
        """
        runner = self.current_runner
        if runner is not None and runner.decision is not None:
            try:
                log.warning("a control window was active; closing it out")
                await runner.close_out()
            except Exception:
                log.exception("close-out failed during shutdown")

        restore = getattr(self.controller, "restore", None)
        if restore is not None:
            try:
                await restore(now=self.clock.now(), reason="process shutting down")
            except Exception:
                log.exception("could not restore the inverter schedule during shutdown")

        for closeable in (self.telemetry, self.forecast, self.controller):
            try:
                await closeable.aclose()
            except Exception:  # noqa: BLE001
                log.debug("error closing %s", closeable, exc_info=True)

    def status(self) -> dict:
        runner = self.current_runner
        return {
            "version": __version__,
            "build": os.environ.get("ZEROHERO_BUILD", "unknown"),
            "site": self.cfg.site.name,
            "now": self.clock.now().isoformat(),
            "active": runner is not None,
            "note": runner.status_note if runner else "idle",
            "setpoint_kw": round(runner.last_setpoint_kw, 3) if runner else 0.0,
            "degraded": runner.degraded if runner else False,
            "decision": runner.decision.model_dump(mode="json", exclude={"slots"}) if runner and runner.decision else None,
            "telemetry": runner.last_telemetry.model_dump(mode="json") if runner and runner.last_telemetry else None,
            "free_window": (
                self.assurance.last_outcome.model_dump(mode="json")
                if self.assurance and self.assurance.last_outcome else None
            ),
            "credit": {
                "secured_so_far": runner.monitor.credit_secured if runner and runner.monitor else None,
                "hours": runner.monitor.report() if runner and runner.monitor else None,
            },
        }
