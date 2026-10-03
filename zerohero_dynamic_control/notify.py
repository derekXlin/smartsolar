"""Daily push messages: yesterday's result in the morning, tonight's plan at 17:50.

Sent with ntfy (https://ntfy.sh): a plain HTTP POST to a topic, delivered to the
ntfy phone app subscribed to it. No account; the topic name is the secret, so it
comes from the environment (NTFY_TOPIC), never from config.yaml.

The morning message waits for GloBird's figures for yesterday, because those
decide the credit. It goes out from the first bill fetch that has them, or at
``notify.morning_deadline`` without them; a short follow-up carries the bill if
it lands later. A small state file makes each message go out once, across
restarts.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import BillRecord, DailyOutcome, FreeWindowOutcome

log = logging.getLogger(__name__)

ENV_TOPIC = "NTFY_TOPIC"


# --------------------------------------------------------------------- sending
class Ntfy:
    def __init__(self, server: str, topic: str, *, transport: Any = None, timeout: float = 15.0) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self.transport = transport
        self.timeout = timeout

    async def send(self, title: str, body: str, *, tags: Iterable[str] = (), priority: int = 3) -> bool:
        """True if ntfy accepted it. Never raises: a lost message must not stop the controller.

        JSON publishing rather than headers: an HTTP header cannot carry a UTF-8 title.
        """
        import httpx

        message = {"topic": self.topic, "title": title, "message": body, "priority": priority, "tags": list(tags)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                resp = await client.post(self.server, json=message)
            if resp.status_code >= 300:
                log.error("ntfy refused the message (HTTP %d)", resp.status_code)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("could not send the ntfy message: %s", exc)
            return False


def ntfy_from_env(server: str, *, transport: Any = None) -> Ntfy | None:
    topic = (os.environ.get(ENV_TOPIC) or "").strip()
    return Ntfy(server, topic, transport=transport) if topic else None


# ----------------------------------------------------------------------- state
class NotifyState:
    """Which day's messages have gone out. Survives restarts."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            self.data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> Any:
        return self.data.get(key)

    def set(self, **values: Any) -> None:
        self.data.update(values)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
        except OSError as exc:
            log.warning("could not save notification state (%s); a message may repeat", exc)


# -------------------------------------------------------------------- composing
def _money(v: float | None) -> str:
    if v is None:
        return "?"
    return f"-${abs(v):.2f}" if v < 0 else f"${v:.2f}"


def control_line(decision: dict[str, Any] | None) -> str | None:
    if not decision:
        return None
    for line in decision.get("rationale") or []:
        if line.startswith("control: "):
            return line[len("control: "):]
    return None


def compose_morning(
    day: date,
    *,
    outcome: DailyOutcome | None,
    bill: BillRecord | None,
    decision: dict[str, Any] | None,
    free_window: FreeWindowOutcome | None,
    overnight: dict[str, Any] | None,
    month_bills: list[BillRecord],
    topup_rate: float,
    health: list[str],
) -> tuple[str, str, list[str]]:
    """Title, body and ntfy tags for yesterday's summary."""
    from .ledger import verdict_of

    d = day.strftime("%a %d %b")
    if bill is not None:
        verdict = "paid" if bill.credit_paid else "NOT paid"
        title = f"ZeroHero {d}: credit {verdict}, day {_money(bill.total_cost_aud)}"
        tags = ["white_check_mark"] if bill.credit_paid else ["x"]
    elif outcome is not None:
        title = f"ZeroHero {d}: {verdict_of(outcome).lower()} (bill pending)"
        tags = ["hourglass"]
    else:
        title = f"ZeroHero {d}: no evening record"
        tags = ["warning"]

    lines: list[str] = []
    if bill is not None:
        sold = ""
        if bill.super_export_topup_aud is not None and topup_rate > 0:
            sold = f", sold {-bill.super_export_topup_aud / topup_rate:.1f} kWh"
        lines.append(
            f"GloBird: credit {'paid' if bill.credit_paid else 'NOT paid'}, day {_money(bill.total_cost_aud)} "
            f"(usage {_money(bill.usage_aud)}, feed-in {_money(bill.solar_aud)}, "
            f"top-up {_money(bill.super_export_topup_aud)}{sold})"
        )
    else:
        lines.append("GloBird: not published yet")

    if decision is not None:
        plan = control_line(decision) or "?"
        lines.append(f"Evening: battery {decision.get('starting_soc_pct', 0):.0f}% at 17:50; {plan}")
    if outcome is not None:
        hours = " / ".join(f"{h.imported_kwh * 1000:.0f}" for h in outcome.hourly_import) or "?"
        lines.append(
            f"Result: sold ~{outcome.exported_kwh:.1f} kWh (estimate), battery {outcome.final_soc_pct:.0f}% at 21:00, "
            f"import {hours} Wh per hour (estimate; limit 30)"
        )
    if overnight:
        lines.append(
            f"Overnight: lowest {overnight['low_soc']:.0f}% at {overnight['low_at']}, "
            f"bought {overnight['import_kwh']:.2f} kWh before {overnight['until']}"
        )
    if free_window is not None:
        lines.append(
            f"Free charge: {free_window.start_soc_pct:.0f}% -> {free_window.final_soc_pct:.0f}% "
            f"(+{free_window.energy_added_kwh:.1f} kWh){'' if free_window.ok else ' - CHECK'}"
        )
    if month_bills:
        paid = sum(b.credit_paid for b in month_bills)
        total = sum(b.total_cost_aud or 0.0 for b in month_bills)
        lines.append(f"{day:%B} so far: credit {paid}/{len(month_bills)} days, total {_money(total)}")
    lines.extend(health)
    return title, "\n".join(lines), tags


def compose_evening(decision: dict[str, Any], *, catch_up: bool) -> tuple[str, str, list[str]]:
    plan = control_line(decision) or "?"
    sell = decision.get("opportunistic_export_kwh") or 0.0
    title = f"Tonight: sell {sell:.1f} kWh" if sell >= 0.05 else "Tonight: self-use, nothing to sell"
    lines = [
        f"Battery {decision.get('starting_soc_pct', 0):.0f}% at {str(decision.get('made_at', ''))[11:16]}"
        + (" (decided after a restart)" if catch_up else ""),
        f"Plan: {plan}",
        f"Expect ~{decision.get('expected_final_soc', 0):.0f}% at 21:00",
    ]
    for line in decision.get("rationale") or []:
        if line.startswith("load learned"):
            lines.append(line[0].upper() + line[1:])
    tags = ["battery"]
    if not decision.get("credit_achievable", True):
        lines.append("WARNING: the credit is not winnable tonight; self-use to protect the battery")
        tags = ["warning"]
    if decision.get("telemetry_assumed"):
        lines.append("WARNING: no battery reading at decision time; the plan is a guess")
        tags = ["warning"]
    elif decision.get("degraded"):
        lines.append("Note: forecast unavailable, fallback curves used")
    return title, "\n".join(lines), tags


# ------------------------------------------------------------- overnight stats
async def overnight_stats(client: Any, sn: str, day: date, now: datetime, tz: Any) -> dict[str, Any] | None:
    """Lowest battery and grid purchase from 21:00 on ``day`` to 11:00 (or now)."""
    start = datetime(day.year, day.month, day.day, 21, tzinfo=tz)
    end = min(now, start + timedelta(hours=14))
    if end <= start:
        return None
    payload = await client.request("/op/v0/device/history/query", {
        "sn": sn, "variables": ["SoC", "gridConsumptionPower"],
        "begin": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000),
    })
    rows = payload[0]["datas"] if isinstance(payload, list) and payload else []
    series = {
        r["variable"]: [(datetime.strptime(p["time"][:19], "%Y-%m-%d %H:%M:%S"), float(p["value"]))
                        for p in r.get("data") or []]
        for r in rows
    }
    soc = series.get("SoC") or []
    if not soc:
        return None
    low_at, low = min(soc, key=lambda p: p[1])
    imp = series.get("gridConsumptionPower") or []
    kwh = sum((v0 + v1) / 2 * (t1 - t0).total_seconds() / 3600
              for (t0, v0), (t1, v1) in zip(imp, imp[1:], strict=False) if (t1 - t0).total_seconds() < 1800)
    return {"low_soc": low, "low_at": low_at.strftime("%H:%M"), "import_kwh": kwh, "until": end.strftime("%H:%M")}


# ------------------------------------------------------------------ assembling
async def morning_message(cfg: Any, ledger: Any, day: date, now: datetime, *,
                          foxess_client: Any = None, health: Iterable[str] = ()) -> tuple[str, str, list[str], bool]:
    """Everything known about ``day``, composed. The last value: was GloBird's bill in it."""
    key = day.isoformat()
    bills = ledger.read_bills()
    bill = bills.get(key)
    outcome = next((o for o in ledger.read_outcomes() if o.date == key), None)
    decisions = ledger.read_decisions(day)
    free = next((f for f in ledger.read_free_windows() if f.date == key), None)
    month = sorted((b for d, b in bills.items() if d[:7] == key[:7] and d <= key), key=lambda b: b.date)
    overnight = None
    sn = getattr(getattr(cfg.providers, "foxess", None), "serial_number", None)
    if foxess_client is not None and sn:
        try:
            overnight = await overnight_stats(foxess_client, sn, day, now, cfg.site.tz)
        except Exception as exc:  # noqa: BLE001 - the summary goes out without it
            log.warning("overnight figures unavailable for the summary: %s", exc)
    title, body, tags = compose_morning(
        day, outcome=outcome, bill=bill, decision=decisions[0] if decisions else None,
        free_window=free, overnight=overnight, month_bills=month,
        topup_rate=cfg.plan.tariff.super_export_topup_aud_per_kwh, health=list(health),
    )
    return title, body, tags, bill is not None


def compose_bill_followup(bill: BillRecord, topup_rate: float) -> tuple[str, str, list[str]]:
    d = date.fromisoformat(bill.date).strftime("%a %d %b")
    sold = ""
    if bill.super_export_topup_aud is not None and topup_rate > 0:
        sold = f", sold {-bill.super_export_topup_aud / topup_rate:.1f} kWh"
    title = f"GloBird {d}: credit {'paid' if bill.credit_paid else 'NOT paid'}, day {_money(bill.total_cost_aud)}"
    body = (f"usage {_money(bill.usage_aud)}, feed-in {_money(bill.solar_aud)}, "
            f"top-up {_money(bill.super_export_topup_aud)}{sold}")
    return title, body, ["white_check_mark"] if bill.credit_paid else ["x"]
