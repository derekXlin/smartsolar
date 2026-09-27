"""Append-only JSONL logging of decisions, samples and daily outcomes.

Requirement 4 asks for a record of every decision, the kWh actually exported, the
final SOC and whether the $1 was secured. Keeping it as JSONL means a day's history
is one `jq` away and the files can be replayed through the simulator later to tune
the strategy against what actually happened.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import DailyOutcome, Decision, FreeWindowOutcome, Telemetry

log = logging.getLogger(__name__)


def _default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON serialisable: {type(obj)}")


class Ledger:
    def __init__(self, outcome_path: Path, decision_path: Path, samples_path: Path | None = None):
        self.outcome_path = Path(outcome_path)
        self.decision_path = Path(decision_path)
        self.samples_path = Path(samples_path) if samples_path else None
        for p in (self.outcome_path, self.decision_path, self.samples_path):
            if p is not None:
                p.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=_default) + "\n")
        except OSError as exc:  # never let logging take down the controller
            log.error("failed to write ledger %s: %s", path, exc)

    def record_decision(self, decision: Decision) -> None:
        payload = decision.model_dump(mode="json")
        payload.pop("slots", None)  # keep the decision log readable; slots go to samples
        payload["slot_count"] = len(decision.slots)
        self._append(self.decision_path, payload)

    def record_sample(self, telemetry: Telemetry, setpoint_kw: float, note: str = "") -> None:
        if self.samples_path is None:
            return
        self._append(
            self.samples_path,
            {
                **telemetry.model_dump(mode="json"),
                "setpoint_kw": round(setpoint_kw, 3),
                "note": note,
            },
        )

    def record_outcome(self, outcome: DailyOutcome) -> None:
        self._append(self.outcome_path, outcome.model_dump(mode="json"))
        verdict = "SECURED" if outcome.credit_secured else "MISSED"
        log.info(
            "day %s: %s the $1 credit | exported %.2f kWh | final SOC %.1f%% | net $%.2f",
            outcome.date, verdict, outcome.exported_kwh, outcome.final_soc_pct,
            outcome.estimated_revenue_aud,
        )

    def record_free_window(self, outcome: FreeWindowOutcome) -> None:
        self._append(self.outcome_path, {"kind": "free_window", **outcome.model_dump(mode="json")})

    def read_free_windows(self, limit: int | None = None) -> list[FreeWindowOutcome]:
        rows = [r for r in self._read_rows() if r.get("kind") == "free_window"]
        out = [FreeWindowOutcome.model_validate(r) for r in rows]
        return out[-limit:] if limit else out

    def _read_rows(self) -> list[dict[str, Any]]:
        if not self.outcome_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.outcome_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log.warning("skipping malformed ledger row: %s", exc)
        return rows

    def read_outcomes(self, limit: int | None = None) -> list[DailyOutcome]:
        if not self.outcome_path.exists():
            return []
        rows: list[DailyOutcome] = []
        for raw in self._read_rows():
            if raw.get("kind") == "free_window":
                continue
            try:
                rows.append(DailyOutcome.model_validate(raw))
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping malformed ledger row: %s", exc)
        return rows[-limit:] if limit else rows
