"""Optional FastAPI status and manual-override endpoints.

FastAPI is an optional dependency — everything else works without it. Import errors
are raised only when `serve` is actually invoked.

    GET  /status              current decision, telemetry and per-hour credit headroom
    GET  /plan                re-run the decision engine right now (read-only, no commands)
    GET  /ledger?limit=14     recent daily outcomes
    GET  /economics           the tariff model and the daily best case
    POST /override            {"power_kw": 4.5}  force a discharge setpoint
    POST /override/clear      hand control back to the loop
    POST /mode                {"mode": "self_consumption"}  emergency mode change
"""

from __future__ import annotations

import logging
from typing import Any

from .economics import best_case_daily_pnl
from .models import BatteryMode, ControlCommand
from .scheduler import ZeroHeroScheduler

log = logging.getLogger(__name__)


def build_app(scheduler: ZeroHeroScheduler) -> Any:
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("fastapi is not installed; run `pip install 'fastapi[standard]'`") from exc

    app = FastAPI(title="ZEROHERO Dynamic Control", version="1.0.0")

    class OverrideRequest(BaseModel):
        power_kw: float

    class ModeRequest(BaseModel):
        mode: BatteryMode

    @app.get("/status")
    async def status() -> dict:
        return scheduler.status()

    @app.get("/plan")
    async def plan() -> dict:
        """Dry run: build a decision from live data without commanding anything."""
        from .runtime import EveningRunner

        runner = EveningRunner(
            scheduler.cfg,
            telemetry=scheduler.telemetry,
            forecast=scheduler.forecast,
            controller=scheduler.controller,
            ledger=None,
            clock=scheduler.clock,
        )
        decision = await runner.make_decision()
        return decision.model_dump(mode="json", exclude={"slots"})

    @app.get("/ledger")
    async def ledger(limit: int = 14) -> list[dict]:
        return [o.model_dump(mode="json", exclude={"decision"}) for o in scheduler.ledger.read_outcomes(limit)]

    @app.get("/economics")
    async def economics() -> dict:
        t = scheduler.cfg.plan.tariff
        best = best_case_daily_pnl(t)
        return {
            "daily_supply_charge_aud": t.daily_supply_charge_aud,
            "in_window_export_aud_per_kwh": t.export_rate(t.zerohero_start),
            "super_export_cap_kwh": t.super_export_cap_kwh,
            "zerohero_credit_aud": t.zerohero_credit_aud,
            "best_case": {"net_aud": round(best.net_aud, 3), "summary": best.format()},
        }

    @app.post("/override")
    async def override(req: OverrideRequest) -> dict:
        runner = scheduler.current_runner
        if runner is None:
            raise HTTPException(409, "no control window is active")
        limit = scheduler.cfg.inverter.ac_limit_kw
        if abs(req.power_kw) > limit:
            raise HTTPException(400, f"power_kw exceeds the {limit} kW inverter limit")
        runner.manual_override_kw = req.power_kw
        log.warning("manual override set to %.2f kW", req.power_kw)
        return {"override_kw": req.power_kw}

    @app.post("/override/clear")
    async def clear_override() -> dict:
        runner = scheduler.current_runner
        if runner is None:
            raise HTTPException(409, "no control window is active")
        runner.manual_override_kw = None
        log.warning("manual override cleared")
        return {"override_kw": None}

    @app.post("/mode")
    async def set_mode(req: ModeRequest) -> dict:
        """Emergency mode change. Still passes through the SafetyWrapper."""
        now = scheduler.clock.now()
        await scheduler.controller.apply(
            ControlCommand(timestamp=now, mode=req.mode, power_kw=0.0, reason="manual API request")
        )
        return {"mode": req.mode.value}

    return app
