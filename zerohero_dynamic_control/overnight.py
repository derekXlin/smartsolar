"""How far the battery falls overnight, learned from history and tomorrow's sun.

The evening sale must leave enough for the night. The old estimate was a fixed
house load (1.4 kW) for the 14 hours to the free window. Seven nights of FoxESS
history (26 Sep-2 Oct 2026) showed two things it got wrong:

* The battery drains faster than the house load suggests: 3.6-4.4 SOC points
  an hour from 21:00 to 04:00, about 1.85 kWh/h of battery energy, while the
  inverter reported ~1.5 kW of house load. The rest is conversion and standby.
  So the drain is learned in battery terms, straight from the SOC.
* The night does not last until 11:00. The battery stops falling once the
  morning sun covers the house: ~07:00 on sunny mornings, ~08:00-11:00 under
  cloud. The low point is at sunrise, and it moves with the weather.

So the energy the night needs is the deepest point of

    battery(t) = battery(21:00) - integral of max(0, drain - forecast PV(t))

between 21:00 and the free window. Backtested on those nights it predicts the
low within 3 SOC points on sunny mornings and 7-9 points too LOW under cloud:
cautious exactly where a wrong forecast would hurt.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)


class OvernightRecord(BaseModel):
    """One night, from 21:00 to the free window, as the battery saw it."""

    date: str                      # the evening the night began
    soc_21: float
    soc_04: float
    drain_kwh_per_h: float
    """Battery energy lost per hour from 21:00 to 04:00: before any sun."""
    low_soc: float
    low_at: str                    # HH:MM next morning
    soc_11: float
    import_kwh: float              # grid purchase 21:00-11:00
    full_at: str | None = None     # HH:MM the free window filled the battery, if it did


def record_from_history(payload: Any, day: date, *, capacity_kwh: float, tz: Any,
                        free_start_hour: int = 11) -> OvernightRecord | None:
    """Build a record from a FoxESS history response covering 21:00 on ``day`` to ~14:00 next day."""
    rows = payload[0]["datas"] if isinstance(payload, list) and payload else []
    series = {
        r["variable"]: [(datetime.strptime(p["time"][:19], "%Y-%m-%d %H:%M:%S"), float(p["value"]))
                        for p in r.get("data") or []]
        for r in rows
    }
    soc = series.get("SoC") or []
    if len(soc) < 10:
        return None
    t21 = datetime(day.year, day.month, day.day, 21)
    t04 = t21 + timedelta(hours=7)
    t11 = datetime(day.year, day.month, day.day, free_start_hour) + timedelta(days=1)

    def at(t: datetime) -> float:
        return min(soc, key=lambda p: abs((p[0] - t).total_seconds()))[1]

    night = [p for p in soc if t21 <= p[0] <= t11]
    if not night or soc[0][0] > t21 + timedelta(minutes=30) or soc[-1][0] < t11:
        return None                       # the history does not cover the night
    low_t, low = min(night, key=lambda p: (p[1], p[0]))
    imp = series.get("gridConsumptionPower") or []
    import_kwh = sum((v0 + v1) / 2 * (b - a).total_seconds() / 3600
                     for (a, v0), (b, v1) in zip(imp, imp[1:], strict=False)
                     if t21 <= a < t11 and (b - a).total_seconds() < 1800)
    full = next((t for t, v in soc if t >= t11 and v >= 99.5), None)
    s21, s04 = at(t21), at(t04)
    return OvernightRecord(
        date=day.isoformat(), soc_21=s21, soc_04=s04,
        drain_kwh_per_h=round(max(0.0, s21 - s04) / 7 * capacity_kwh / 100, 3),
        low_soc=low, low_at=low_t.strftime("%H:%M"), soc_11=at(t11), import_kwh=round(import_kwh, 3),
        full_at=full.strftime("%H:%M") if full else None,
    )


async def fetch_night(client: Any, sn: str, day: date, *, capacity_kwh: float, tz: Any) -> OvernightRecord | None:
    """One FoxESS history call: 20:30 on ``day`` to 14:30 the next day."""
    start = datetime(day.year, day.month, day.day, 20, 30, tzinfo=tz)
    payload = await client.request("/op/v0/device/history/query", {
        "sn": sn, "variables": ["SoC", "gridConsumptionPower"],
        "begin": int(start.timestamp() * 1000), "end": int((start + timedelta(hours=18)).timestamp() * 1000),
    })
    return record_from_history(payload, day, capacity_kwh=capacity_kwh, tz=tz)


def learned_drain(records: list[OvernightRecord], *, before: date, days: int = 7,
                  min_nights: int = 3) -> tuple[float, int] | None:
    """Median night drain (kWh/h) of the last ``days`` nights before ``before``."""
    recent = sorted((r for r in records if before - timedelta(days=days) <= date.fromisoformat(r.date) < before),
                    key=lambda r: r.date)
    rates = [r.drain_kwh_per_h for r in recent if r.drain_kwh_per_h > 0]
    if len(rates) < min_nights:
        return None
    return statistics.median(rates), len(rates)


def overnight_need(drain_kwh_per_h: float, pv_kw_at: Any, start: datetime, end: datetime,
                   *, step_minutes: int = 15) -> tuple[float, datetime]:
    """Battery energy the night takes before it stops falling, and when that is.

    Steps from ``start`` (21:00) to ``end`` (the free window), each step draining
    ``drain - PV`` (never negative). The answer is the deepest point reached: the
    morning sun can lift the battery afterwards, but it cannot undo the low.
    """
    t, cum, worst, worst_t = start, 0.0, 0.0, start
    step = timedelta(minutes=step_minutes)
    while t < end:
        cum += max(0.0, drain_kwh_per_h - max(0.0, pv_kw_at(t))) * step_minutes / 60
        t += step
        if cum > worst + 1e-9:
            worst, worst_t = cum, t
    return worst, worst_t


async def model_pv_curves(cfg: Any, start: datetime, end: datetime, models: list[str]) -> dict[str, Any]:
    """Tomorrow morning's PV under each weather model, as kW-at-time functions.

    One Open-Meteo request for all models (hourly tilted irradiance; not every
    model has the 15-minute field). A model that returns nothing is left out.
    """
    import httpx

    from .curves import ForecastCurve
    from .data_providers.open_meteo import ENDPOINT, OpenMeteoForecastProvider
    from .models import ForecastPoint

    if not models:
        return {}
    async with httpx.AsyncClient(timeout=cfg.forecast.timeout_seconds) as client:
        resp = await client.get(ENDPOINT, params={
            "latitude": cfg.site.latitude, "longitude": cfg.site.longitude, "timezone": cfg.site.timezone,
            "hourly": "global_tilted_irradiance", "forecast_days": 2, "models": ",".join(models),
            "tilt": cfg.forecast.array_tilt_deg, "azimuth": OpenMeteoForecastProvider(cfg)._azimuth(),
        })
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})
    times = [datetime.fromisoformat(t).replace(tzinfo=cfg.site.tz) for t in hourly.get("time", [])]
    rating = cfg.forecast.pv_rating_kw
    curves = {}
    for model in models:
        values = hourly.get(f"global_tilted_irradiance_{model}") or []
        pts = [ForecastPoint(timestamp=t, solar_kw=min(rating, rating * v / 1000 * 0.88))
               for t, v in zip(times, values, strict=False)
               if v is not None and start - timedelta(hours=1) <= t <= end + timedelta(hours=1)]
        if len(pts) >= 4:
            curves[model] = ForecastCurve(pts, "solar_kw")
    return curves
