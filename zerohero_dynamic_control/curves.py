"""Sampling helpers that turn discrete forecast points into continuous curves."""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence
from datetime import datetime

from .models import ForecastPoint

KwAt = Callable[[datetime], float]


class ForecastCurve:
    """Linear interpolation over an ordered list of forecast points.

    Forecast feeds arrive on coarse grids (Solcast 30 min, Open-Meteo 60 min) while
    the planner works in 5 minute slots, so we interpolate rather than step — a step
    function would produce a visible sawtooth in the planned discharge profile.
    """

    def __init__(self, points: Sequence[ForecastPoint], field: str) -> None:
        self._pts = sorted(points, key=lambda p: p.timestamp)
        self._ts = [p.timestamp for p in self._pts]
        self._vals = [float(getattr(p, field)) for p in self._pts]

    def __bool__(self) -> bool:
        return bool(self._pts)

    def __call__(self, when: datetime) -> float:
        if not self._pts:
            return 0.0
        if when <= self._ts[0]:
            return self._vals[0]
        if when >= self._ts[-1]:
            return self._vals[-1]
        i = bisect.bisect_right(self._ts, when)
        t0, t1 = self._ts[i - 1], self._ts[i]
        v0, v1 = self._vals[i - 1], self._vals[i]
        span = (t1 - t0).total_seconds()
        if span <= 0:
            return v1
        frac = (when - t0).total_seconds() / span
        return v0 + (v1 - v0) * frac


def constant_curve(value: float) -> KwAt:
    def _c(_when: datetime) -> float:
        return value

    return _c


def integrate(curve: KwAt, start: datetime, end: datetime, step_minutes: int = 5) -> float:
    """Energy (kWh) under a kW curve, sampled at slot midpoints."""
    if end <= start:
        return 0.0
    total_minutes = (end - start).total_seconds() / 60.0
    n = max(1, int(round(total_minutes / step_minutes)))
    dt_h = total_minutes / n / 60.0
    energy = 0.0
    for i in range(n):
        frac = (i + 0.5) / n
        mid = start + (end - start) * frac
        energy += curve(mid) * dt_h
    return energy
