"""Evening load learned from the controller's own recent readings.

The static profile in config.yaml is a guess (2.7 / 2.9 / 2.4 kW for 18:00, 19:00
and 20:00 at this site). Over 28-30 Sep the house actually drew 1.9-2.4 kW. A
forecast ~2 kWh too high over the window makes the 17:50 plan keep energy it
does not need, and every 15-minute replan then finds the surplus and sells it
in a short burst: five extra mode switches on 30 Sep.

Every evening the loop already records the site's load in samples.jsonl, so the
recent past is the best available forecast. For each 15-minute slot of the day
this takes the mean over the last ``days`` evenings (one mean per evening first,
so an evening polled more often does not count more), and falls back to the
wrapped provider for any slot with fewer than ``min_days`` evenings of data.
The mean, not a percentile: the plan budgets energy, and the reserve and the
live energy guard exist for evenings that run above it.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import ForecastPoint
from .base import ForecastProvider

log = logging.getLogger(__name__)


class LearnedLoadForecastProvider(ForecastProvider):
    def __init__(
        self,
        inner: ForecastProvider,
        samples_path: Path,
        *,
        tz: ZoneInfo,
        days: int = 7,
        min_days: int = 3,
        slot_minutes: int = 15,
    ) -> None:
        self.inner = inner
        self.samples_path = Path(samples_path)
        self.tz = tz
        self.days = days
        self.min_days = min_days
        self.slot_minutes = slot_minutes
        self.name = f"learned-load({inner.name})"
        self._cache: tuple[date, dict[int, tuple[float, int]]] | None = None
        self.load_note: str | None = None
        """One line for the decision's rationale describing where the load came from."""

    def _slot(self, when: datetime) -> int:
        local = when.astimezone(self.tz)
        return (local.hour * 60 + local.minute) // self.slot_minutes

    def profile(self, today: date) -> dict[int, tuple[float, int]]:
        """{slot: (kW, evenings)} from the ``days`` evenings before ``today``.

        Learned once per day: replans reuse it, and today's partial evening is left out.
        """
        if self._cache is not None and self._cache[0] == today:
            return self._cache[1]
        first = today - timedelta(days=self.days)
        # (day, slot) -> {measurement time: kW}; keyed by time so a snapshot polled
        # five times counts once.
        seen: dict[tuple[date, int], dict[datetime, float]] = defaultdict(dict)
        if self.samples_path.exists():
            with self.samples_path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        raw = json.loads(line)
                        when = datetime.fromisoformat(raw.get("measured_at") or raw["timestamp"])
                        day = when.astimezone(self.tz).date()
                        if first <= day < today:
                            seen[(day, self._slot(when))][when] = float(raw["load_kw"])
                    except (ValueError, KeyError, TypeError):
                        continue
        per_slot: dict[int, list[float]] = defaultdict(list)
        for (_day, slot), readings in seen.items():
            per_slot[slot].append(sum(readings.values()) / len(readings))
        profile = {s: (sum(v) / len(v), len(v)) for s, v in per_slot.items() if len(v) >= self.min_days}
        self._cache = (today, profile)
        return profile

    async def solar(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        return await self.inner.solar(start, end)

    async def load(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        base = await self.inner.load(start, end)
        profile = self.profile(start.astimezone(self.tz).date())
        out, used = [], []
        for p in base:
            hit = profile.get(self._slot(p.timestamp))
            if hit is None:
                out.append(p)
            else:
                out.append(p.model_copy(update={"load_kw": hit[0]}))
                used.append((p.timestamp.astimezone(self.tz).hour, hit))
        if used:
            by_hour: dict[int, list[float]] = defaultdict(list)
            for hour, (kw, _) in used:
                by_hour[hour].append(kw)
            evenings = max(n for _, (_, n) in used)
            self.load_note = (
                f"load learned from up to {evenings} recent evenings: "
                + ", ".join(f"{h:02d}:00 {sum(v) / len(v):.1f} kW" for h, v in sorted(by_hour.items()))
            )
        else:
            self.load_note = None
        return out

    async def aclose(self) -> None:
        await self.inner.aclose()
