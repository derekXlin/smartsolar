"""Abstract data provider interfaces.

Everything the controller needs to know about the site comes through these three
protocols. Keeping them narrow means a new inverter brand is a single new class,
and the decision engine never learns what hardware it is talking to.
"""

from __future__ import annotations

import abc
import logging
from datetime import datetime

from ..models import ForecastPoint, Telemetry

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a reading. Callers must degrade, not crash."""


class TelemetryProvider(abc.ABC):
    """Live site measurements. Implementations must be safe to call every minute."""

    name: str = "telemetry"

    @abc.abstractmethod
    async def read(self, now: datetime) -> Telemetry:
        ...

    async def aclose(self) -> None:
        return None


class ForecastProvider(abc.ABC):
    """Solar and/or load forecasts."""

    name: str = "forecast"

    @abc.abstractmethod
    async def solar(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        ...

    async def load(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        """Default: no load forecast. The engine falls back to a static average."""
        return []

    async def aclose(self) -> None:
        return None


class CachingTelemetryProvider(TelemetryProvider):
    """Wraps a provider so a transient API failure yields the last good reading.

    This is the first line of graceful degradation: a single dropped poll should not
    make the control loop blind. Readings older than ``max_age_seconds`` are marked
    ``stale`` so the loop can escalate to the blind fallback profile instead of
    steering on numbers that no longer describe the site.
    """

    def __init__(self, inner: TelemetryProvider, max_age_seconds: float = 600.0) -> None:
        self.inner = inner
        self.max_age_seconds = max_age_seconds
        self.name = f"caching({inner.name})"
        self._last: Telemetry | None = None
        self.consecutive_failures = 0

    async def read(self, now: datetime) -> Telemetry:
        try:
            reading = await self.inner.read(now)
            self._last = reading
            self.consecutive_failures = 0
            return reading
        except Exception as exc:  # noqa: BLE001 - provider failures must never propagate
            self.consecutive_failures += 1
            log.warning("telemetry read failed (%s): %s", self.consecutive_failures, exc)
            if self._last is None:
                raise ProviderError("no telemetry has ever been read") from exc
            age = (now - self._last.timestamp).total_seconds()
            stale = self._last.model_copy(update={"stale": True})
            if age > self.max_age_seconds:
                raise ProviderError(f"telemetry stale by {age:.0f}s") from exc
            return stale

    async def aclose(self) -> None:
        await self.inner.aclose()
