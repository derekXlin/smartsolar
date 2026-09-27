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

    def __init__(
        self,
        inner: TelemetryProvider,
        max_age_seconds: float = 600.0,
        *,
        max_soc_rate_pct_per_min: float | None = None,
        max_rejects: int = 3,
        soc_resolution_pct: float = 1.0,
    ) -> None:
        self.inner = inner
        self.max_age_seconds = max_age_seconds
        self.max_soc_rate_pct_per_min = max_soc_rate_pct_per_min
        self.max_rejects = max_rejects
        self.soc_resolution_pct = soc_resolution_pct
        self.name = f"caching({inner.name})"
        self._last: Telemetry | None = None
        self.consecutive_failures = 0
        self.consecutive_rejects = 0
        self.rejected_samples = 0

    def _implausible(self, reading: Telemetry) -> str | None:
        """Reject SOC jumps the hardware could not physically produce.

        Observed on a real FoxESS cloud feed: two identical SOC samples 90 s
        apart reading 9 points BELOW the true value, then a 21-point jump back
        in six minutes. A 10 kW inverter into a 47 kWh pack can move SOC by at
        most ~0.35 points a minute, so that jump was impossible and the low
        readings were a stale snapshot, not a discharge.

        This matters because of when it could strike. A bogus LOW reading at
        17:50 makes the engine conclude the window is unwinnable and abandon a
        credit it could have won; a bogus HIGH one makes it over-export and lose
        both the credit and the charge. Neither is recoverable after the fact,
        so a physically impossible sample is discarded in favour of the last
        good one.

        The ceiling includes one reporting step. FoxESS reports SOC in whole
        points, so a pack at 55.5% ticking to 54.4% reads as a full point in a
        minute. Without the step allowance every ordinary tick was rejected — 19
        times on the first live evening — and each rejection threw away the fresh
        snapshot and dropped the loop to its blind fallback. At 18:50 the discarded
        snapshot was the one showing a 4 kW load spike.
        """
        if self.max_soc_rate_pct_per_min is None or self._last is None:
            return None
        dt_min = (reading.timestamp - self._last.timestamp).total_seconds() / 60.0
        if dt_min <= 0 or dt_min > 15:
            # A long gap (restart, outage) legitimately allows a large change.
            return None
        delta = abs(reading.soc_pct - self._last.soc_pct)
        # 50% headroom on the physics, plus one reporting step of quantisation.
        ceiling = self.max_soc_rate_pct_per_min * dt_min * 1.5 + self.soc_resolution_pct
        if delta > ceiling:
            return (
                f"SOC moved {delta:.1f} points in {dt_min:.1f} min "
                f"({self._last.soc_pct:.0f}% -> {reading.soc_pct:.0f}%); the inverter "
                f"can manage at most {ceiling:.1f}"
            )
        return None

    async def read(self, now: datetime) -> Telemetry:
        try:
            reading = await self.inner.read(now)
            self.consecutive_failures = 0

            reason = self._implausible(reading)
            if reason is not None and self.consecutive_rejects < self.max_rejects:
                # Hold the last good reading, but only briefly: if the "impossible"
                # value keeps coming back it is more likely our baseline was wrong
                # than that the inverter is lying, so give in after max_rejects.
                self.consecutive_rejects += 1
                self.rejected_samples += 1
                log.warning("rejecting implausible telemetry (%d/%d): %s",
                            self.consecutive_rejects, self.max_rejects, reason)
                assert self._last is not None
                return self._last.model_copy(update={"timestamp": now, "stale": True})
            if reason is not None:
                log.warning("accepting previously rejected SOC after %d samples — "
                            "treating the earlier baseline as wrong", self.consecutive_rejects)

            self.consecutive_rejects = 0
            self._last = reading
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
