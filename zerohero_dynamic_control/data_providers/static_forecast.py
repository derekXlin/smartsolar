"""Dependency-free forecasts: clear-sky geometry scaled by a cloud factor.

This is the fallback that keeps the controller working when Solcast or Open-Meteo is
unreachable at 17:50. It is deliberately conservative — it is better to under-predict
residual solar (and so plan for a little more battery discharge) than to over-predict
and end up importing, because an over-prediction costs the whole $1 credit while an
under-prediction costs a fraction of a kWh.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..config import AppConfig
from ..models import ForecastPoint
from ..solar_geometry import clear_sky_pv_kw
from .base import ForecastProvider


class StaticForecastProvider(ForecastProvider):
    name = "static"

    def __init__(self, config: AppConfig, cloud_factor: float = 0.85, step_minutes: int = 15) -> None:
        self.cfg = config
        self.cloud_factor = cloud_factor
        self.step_minutes = step_minutes

    async def solar(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        site = self.cfg.site
        pts: list[ForecastPoint] = []
        t = start
        while t <= end:
            kw = clear_sky_pv_kw(t, site.latitude, site.longitude, self.cfg.forecast.pv_rating_kw)
            pts.append(ForecastPoint(timestamp=t, solar_kw=kw * self.cloud_factor))
            t += timedelta(minutes=self.step_minutes)
        return pts

    async def load(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        """Hour-of-day load profile from config, else a flat evening average."""
        profile = self.cfg.forecast.load_profile_kw
        pts: list[ForecastPoint] = []
        t = start
        while t <= end:
            if profile:
                kw = float(profile.get(str(t.hour), self.cfg.forecast.static_evening_load_kw))
            else:
                kw = self.cfg.forecast.static_evening_load_kw
            pts.append(ForecastPoint(timestamp=t, load_kw=kw))
            t += timedelta(minutes=self.step_minutes)
        return pts
