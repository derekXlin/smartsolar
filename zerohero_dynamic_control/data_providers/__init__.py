"""Data provider implementations."""

from __future__ import annotations

from ..config import AppConfig
from .base import (
    CachingTelemetryProvider,
    ForecastProvider,
    ProviderError,
    TelemetryProvider,
)
from .static_forecast import StaticForecastProvider

__all__ = [
    "TelemetryProvider",
    "ForecastProvider",
    "ProviderError",
    "CachingTelemetryProvider",
    "StaticForecastProvider",
    "build_forecast_provider",
    "build_telemetry_provider",
]


def build_telemetry_provider(cfg: AppConfig) -> TelemetryProvider:
    """Construct the configured live telemetry source."""
    if cfg.providers.battery == "foxess":
        from ..foxess_client import CallBudget, FoxESSClient
        from .foxess import FoxESSTelemetryProvider

        fox = cfg.providers.foxess
        if not fox.serial_number:
            raise ProviderError(
                "providers.foxess.serial_number is required; "
                "run `zerohero foxess-discover` to find it"
            )
        client = FoxESSClient(
            fox.api_key or "",
            base_url=fox.base_url,
            timezone=cfg.site.timezone,
            budget=CallBudget(daily_limit=fox.daily_call_limit, reserve=fox.call_reserve),
        )
        return FoxESSTelemetryProvider(cfg, client, fox.serial_number)

    raise ProviderError(
        f"providers.battery={cfg.providers.battery!r} has no live implementation; "
        "use 'foxess', or 'simulated' for the synthetic site"
    )


def build_forecast_provider(cfg: AppConfig) -> ForecastProvider:
    if cfg.forecast.provider == "open_meteo":
        from .open_meteo import OpenMeteoForecastProvider

        return OpenMeteoForecastProvider(cfg)
    # 'solcast' would slot in here; 'static' and 'simulated' both use the clear-sky model.
    return StaticForecastProvider(cfg)
