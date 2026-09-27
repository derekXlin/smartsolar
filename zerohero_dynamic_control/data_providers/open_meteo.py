"""Open-Meteo solar forecast provider (free, no API key).

Uses the shortwave/direct radiation fields and a flat efficiency factor rather than a
full plane-of-array transposition — accurate enough to decide whether to plan for
3 kWh or 0.3 kWh of residual solar after 18:00, which is the only question we are
asking it. Falls back to the static provider on any error.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import AppConfig
from ..models import ForecastPoint
from .base import ForecastProvider, ProviderError
from .static_forecast import StaticForecastProvider

log = logging.getLogger(__name__)

ENDPOINT = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoForecastProvider(ForecastProvider):
    name = "open_meteo"

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self._fallback = StaticForecastProvider(config)
        self._client = None

    async def _get_client(self):
        if self._client is None:
            try:
                import httpx  # imported lazily so the package works without it
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise ProviderError("httpx is not installed; using static forecast") from exc
            self._client = httpx.AsyncClient(timeout=self.cfg.forecast.timeout_seconds)
        return self._client

    def _azimuth(self) -> float:
        """Panel direction in Open-Meteo's convention.

        Their docs: "0 south, -90 east, 90 west, +/-180 north" — and there is NO
        automatic hemisphere adjustment. Passing 0 from Sydney therefore models a
        SOUTH-facing array, which points away from the sun and under-forecasts
        badly. Below the equator the sun tracks through the north, so a
        north-facing array is 180.
        """
        configured = self.cfg.forecast.array_azimuth_deg
        if configured is not None:
            return configured
        return 180.0 if self.cfg.site.latitude < 0 else 0.0

    async def solar(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        try:
            client = await self._get_client()
            resp = await client.get(
                ENDPOINT,
                params={
                    "latitude": self.cfg.site.latitude,
                    "longitude": self.cfg.site.longitude,
                    "minutely_15": "global_tilted_irradiance",
                    "timezone": self.cfg.site.timezone,
                    "forecast_days": 2,
                    "tilt": self.cfg.forecast.array_tilt_deg,
                    "azimuth": self._azimuth(),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            block = data.get("minutely_15", {})
            times = block.get("time", [])
            gti = block.get("global_tilted_irradiance", [])
            if not times:
                raise ProviderError("open-meteo returned no data")

            tz = self.cfg.site.tz
            rating = self.cfg.forecast.pv_rating_kw
            pts: list[ForecastPoint] = []
            for ts, irr in zip(times, gti, strict=False):
                if irr is None:
                    continue
                when = datetime.fromisoformat(ts).replace(tzinfo=tz)
                if when < start - timedelta(minutes=30) or when > end + timedelta(minutes=30):
                    continue
                # 1000 W/m^2 on the array == nameplate output, derated for
                # temperature, soiling and inverter efficiency.
                kw = min(rating, rating * (irr / 1000.0) * 0.88)
                pts.append(ForecastPoint(timestamp=when, solar_kw=max(0.0, kw)))
            if not pts:
                raise ProviderError("open-meteo returned nothing inside the window")
            return pts
        except Exception as exc:  # noqa: BLE001
            log.warning("open-meteo forecast failed (%s); falling back to clear-sky model", exc)
            return await self._fallback.solar(start, end)

    async def load(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        return await self._fallback.load(start, end)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
