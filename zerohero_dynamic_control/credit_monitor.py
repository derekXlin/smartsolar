"""Tracks compliance with the ZeroHero rule in real time.

The rule is per-hour, not per-window: grid import must stay under 0.03 kWh in EVERY
clock hour of 18:00-21:00. That distinction matters enormously for control. A window
total would let us absorb a 2 kW spike at 18:05 and make it up later; a per-hour
budget means that spike burns the 18:00-19:00 hour outright and there is nothing to
"make up" — the hour is simply lost, and with it the whole $1.

0.03 kWh is a tiny allowance: 1.8 kW for one minute, or 180 W for ten. So the monitor
integrates import continuously and reports headroom as a fraction, letting the control
loop escalate its safety margin long before the limit is reached.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .models import HourImport

log = logging.getLogger(__name__)


class CreditMonitor:
    """Integrates grid import into per-clock-hour buckets across the credit window."""

    def __init__(self, window_start: datetime, window_end: datetime, limit_kwh_per_hour: float = 0.03):
        self.window_start = window_start
        self.window_end = window_end
        self.limit = limit_kwh_per_hour
        self.buckets: dict[datetime, HourImport] = {}
        self._last_sample: tuple[datetime, float] | None = None

        h = window_start.replace(minute=0, second=0, microsecond=0)
        while h < window_end:
            self.buckets[h] = HourImport(hour_start=h, limit_kwh=limit_kwh_per_hour)
            h += timedelta(hours=1)

    # ------------------------------------------------------------------ ingest
    def observe(self, when: datetime, grid_kw: float) -> None:
        """Add a telemetry sample. Import energy is integrated trapezoidally.

        Trapezoidal rather than rectangular because with a 60 s poll and a 0.03 kWh
        budget, treating a ramp as a step can misestimate the hour by a third of the
        entire allowance.
        """
        import_kw = max(0.0, grid_kw)
        if self._last_sample is not None:
            prev_t, prev_kw = self._last_sample
            dt_h = (when - prev_t).total_seconds() / 3600.0
            if 0 < dt_h < 1.0:  # ignore absurd gaps; they are handled as data loss
                energy = (prev_kw + import_kw) / 2.0 * dt_h
                self._add(prev_t, when, energy)
        self._last_sample = (when, import_kw)

    def _add(self, start: datetime, end: datetime, energy_kwh: float) -> None:
        """Attribute energy to hour buckets, splitting across an hour boundary."""
        if energy_kwh <= 0:
            return
        bucket_start = start.replace(minute=0, second=0, microsecond=0)
        boundary = bucket_start + timedelta(hours=1)
        if end <= boundary or (end - start).total_seconds() <= 0:
            if bucket_start in self.buckets:
                self.buckets[bucket_start].imported_kwh += energy_kwh
            return
        total = (end - start).total_seconds()
        first_frac = (boundary - start).total_seconds() / total
        if bucket_start in self.buckets:
            self.buckets[bucket_start].imported_kwh += energy_kwh * first_frac
        self._add(boundary, end, energy_kwh * (1.0 - first_frac))

    # ------------------------------------------------------------------ report
    def current_bucket(self, when: datetime) -> HourImport | None:
        return self.buckets.get(when.replace(minute=0, second=0, microsecond=0))

    def headroom_fraction(self, when: datetime) -> float:
        """1.0 = the current hour is untouched, 0.0 = the allowance is spent."""
        bucket = self.current_bucket(when)
        if bucket is None:
            return 1.0
        return max(0.0, min(1.0, bucket.headroom_kwh / self.limit))

    @property
    def total_import_kwh(self) -> float:
        return sum(b.imported_kwh for b in self.buckets.values())

    @property
    def credit_secured(self) -> bool:
        """True only if every hour of the window stayed under the limit."""
        return bool(self.buckets) and not any(b.breached for b in self.buckets.values())

    def breached_hours(self) -> list[HourImport]:
        return [b for b in self.buckets.values() if b.breached]

    def report(self) -> str:
        parts = [
            f"{b.hour_start:%H:%M} {b.imported_kwh * 1000:6.1f} Wh"
            + ("  BREACH" if b.breached else f"  ({b.headroom_kwh * 1000:5.1f} Wh left)")
            for b in sorted(self.buckets.values(), key=lambda x: x.hour_start)
        ]
        return " | ".join(parts)
