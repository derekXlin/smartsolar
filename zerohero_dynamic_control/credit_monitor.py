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

from .models import SUSTAINED_IMPORT_KW, HourImport

log = logging.getLogger(__name__)


MAX_SAMPLE_GAP = timedelta(minutes=15)
"""A longer gap between samples is data loss: neither integrated nor counted as
watched. Matches the telemetry cache, which gives up on readings older than this."""


class CreditMonitor:
    """Integrates grid import into per-clock-hour buckets across the credit window."""

    def __init__(self, window_start: datetime, window_end: datetime, limit_kwh_per_hour: float = 0.03):
        self.window_start = window_start
        self.window_end = window_end
        self.limit = limit_kwh_per_hour
        self.buckets: dict[datetime, HourImport] = {}
        self._last_sample: tuple[datetime, float, bool] | None = None

        h = window_start.replace(minute=0, second=0, microsecond=0)
        while h < window_end:
            span = min(h + timedelta(hours=1), window_end) - max(h, window_start)
            self.buckets[h] = HourImport(
                hour_start=h, limit_kwh=limit_kwh_per_hour,
                span_minutes=span.total_seconds() / 60.0, confirmed_import_kwh=0.0,
            )
            h += timedelta(hours=1)

    # ------------------------------------------------------------------ ingest
    def observe(self, when: datetime, grid_kw: float, *, forced: bool = True) -> None:
        """Add a telemetry sample. Import energy is integrated trapezoidally.

        Trapezoidal rather than rectangular because with a 60 s poll and a 0.03 kWh
        budget, treating a ramp as a step can misestimate the hour by a third of the
        entire allowance.

        ``when`` should be the MEASUREMENT time (Telemetry.observed_time). Two rules
        follow from it:
          * a sample measured before the window opens says nothing about the window.
            On 29 Sep the first poll at 18:00 returned a 17:5x snapshot of the house
            importing 0.46 kW before the window; held to the next snapshot it put
            36 Wh into the 18:00 hour while the battery was exporting 8 kW;
          * a snapshot polled again is not new information, so a repeat (same or
            older time) is ignored rather than integrated as continued import.

        ``forced`` says whether the battery was force-discharging when the sample
        was measured; unknown counts as forced. Each sample owns half of the
        interval on either side of it. Its share counts as CONFIRMED import if it
        was forced, or if both ends of the interval show sustained import: see
        HourImport.clearly_breached for why the mode decides.
        """
        if when < self.window_start:
            return
        import_kw = max(0.0, grid_kw)
        if self._last_sample is not None:
            prev_t, prev_kw, prev_forced = self._last_sample
            if when <= prev_t:
                return
            gap = when - prev_t
            if timedelta(0) < gap <= MAX_SAMPLE_GAP:
                hours = gap.total_seconds() / 3600.0
                energy = (prev_kw + import_kw) / 2.0 * hours
                sustained = prev_kw >= SUSTAINED_IMPORT_KW and import_kw >= SUSTAINED_IMPORT_KW
                confirmed = (
                    (prev_kw if prev_forced or sustained else 0.0)
                    + (import_kw if forced or sustained else 0.0)
                ) / 2.0 * hours
                self._add(prev_t, when, energy, confirmed)
        self._last_sample = (when, import_kw, forced)

    def _add(self, start: datetime, end: datetime, energy_kwh: float, confirmed_kwh: float = 0.0) -> None:
        """Attribute energy and watched time to hour buckets, split at hour boundaries."""
        total = (end - start).total_seconds()
        t = start
        while t < end:
            bucket_start = t.replace(minute=0, second=0, microsecond=0)
            piece_end = min(end, bucket_start + timedelta(hours=1))
            bucket = self.buckets.get(bucket_start)
            if bucket is not None:
                # Only the part of the interval inside the window counts as watched.
                lo, hi = max(t, self.window_start), min(piece_end, self.window_end)
                if hi > lo:
                    bucket.observed_minutes += (hi - lo).total_seconds() / 60.0
                share = (piece_end - t).total_seconds() / total
                if energy_kwh > 0:
                    bucket.imported_kwh += energy_kwh * share
                if confirmed_kwh > 0:
                    bucket.confirmed_import_kwh = (bucket.confirmed_import_kwh or 0.0) + confirmed_kwh * share
            t = piece_end

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
    def breach_free(self) -> bool:
        """No hour has gone over the limit so far. Says nothing about unwatched time."""
        return not any(b.breached for b in self.buckets.values())

    @property
    def credit_verified(self) -> bool:
        """Every hour of the window was actually watched."""
        return bool(self.buckets) and all(b.verified for b in self.buckets.values())

    @property
    def credit_secured(self) -> bool:
        """True only if every hour stayed under the limit AND was watched.

        The buckets exist from the start, so without the coverage check a monitor
        that never saw a single sample reported a clean pass. That is exactly the
        ledger row a mid-window restart wrote on the first live evening.
        """
        return self.breach_free and self.credit_verified

    def breached_hours(self) -> list[HourImport]:
        return [b for b in self.buckets.values() if b.breached]

    def unverified_hours(self) -> list[HourImport]:
        return [b for b in self.buckets.values() if not b.verified]

    def report(self) -> str:
        def one(b: HourImport) -> str:
            head = f"{b.hour_start:%H:%M} {b.imported_kwh * 1000:6.1f} Wh"
            if b.clearly_breached:
                return head + "  BREACH"
            if b.breached:
                return head + f"  OVER? ({(b.confirmed_import_kwh or 0) * 1000:.0f} Wh confirmed; the bill decides)"
            if not b.verified:
                return head + f"  UNVERIFIED ({b.observed_minutes:.0f}/{b.span_minutes:.0f} min seen)"
            return head + f"  ({b.headroom_kwh * 1000:5.1f} Wh left)"

        return " | ".join(one(b) for b in sorted(self.buckets.values(), key=lambda x: x.hour_start))
