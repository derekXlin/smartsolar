"""Abstract battery controller.

The battery brand is not yet known, so this is the seam the whole system is built
around. A concrete controller has to answer only three questions:

    set_mode(mode)          — put the inverter into self-consumption / force-export /
                              force-charge / hold
    set_power(kw)           — command a discharge (positive) or charge (negative) rate
    capabilities()          — tell the control loop what it can actually do

That last one matters. Some hardware cannot take a continuous power setpoint at all;
it only understands "discharge to grid at the configured rate" (many TOU-mode
inverters) or "reserve = N%". The control loop reads ``ControllerCapabilities`` and
falls back from fine power modulation to coarse SOC-target control automatically,
so a limited controller degrades in quality rather than failing outright.

Mapping notes for the likely candidates:

  Tesla Powerwall (Fleet API / pypowerwall / Netzero)
      FORCE_EXPORT   -> operation mode "autonomous" (Time-Based Control) with an
                        export-everything setting and a peak price window covering
                        18:00-21:00; or Netzero's direct "export" command.
      power setpoint -> not natively exposed by the Fleet API; use backup_reserve_percent
                        as the lever and set supports_power_setpoint=False.
  Home Assistant
      Any of the above via number/select entities — see HomeAssistantController.
  Modbus (Sungrow, GoodWe, Fronius Gen24, Sigenergy)
      FORCE_EXPORT   -> write the "forced discharge" enum plus a power register.
                        These DO support a true power setpoint.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from datetime import datetime

from ..models import BatteryMode, ControlCommand

log = logging.getLogger(__name__)


class ControllerError(RuntimeError):
    """Raised when a command cannot be delivered to the hardware."""


@dataclass(frozen=True)
class ControllerCapabilities:
    supports_power_setpoint: bool = True
    """True if the controller can be given an arbitrary kW discharge rate."""
    supports_soc_target: bool = False
    """True if the controller can be told 'stop at N% SOC'."""
    min_command_interval_seconds: float = 30.0
    """Rate limit imposed by the vendor API. The loop will not command faster."""
    power_resolution_kw: float = 0.1
    max_power_kw: float = 10.0


class BatteryController(abc.ABC):
    """Vendor-neutral battery control surface."""

    name: str = "abstract"

    def __init__(self) -> None:
        self.last_command: ControlCommand | None = None
        self.command_log: list[ControlCommand] = []

    def capabilities(self) -> ControllerCapabilities:
        return ControllerCapabilities()

    @abc.abstractmethod
    async def set_mode(self, mode: BatteryMode, *, now: datetime, reason: str = "") -> None:
        ...

    @abc.abstractmethod
    async def set_power(self, power_kw: float, *, now: datetime, reason: str = "") -> None:
        ...

    async def set_soc_target(self, soc_pct: float, *, now: datetime, reason: str = "") -> None:
        """Optional: only meaningful when capabilities().supports_soc_target."""
        raise NotImplementedError

    async def apply(self, command: ControlCommand) -> None:
        """Issue a full command (mode + power) and record it."""
        if self.last_command is None or self.last_command.mode is not command.mode:
            await self.set_mode(command.mode, now=command.timestamp, reason=command.reason)
        if self.capabilities().supports_power_setpoint:
            await self.set_power(command.power_kw, now=command.timestamp, reason=command.reason)
        self.last_command = command
        self.command_log.append(command)

    async def health_check(self) -> bool:
        """Cheap liveness probe, run before the window opens."""
        return True

    async def aclose(self) -> None:
        return None


class SafetyWrapper(BatteryController):
    """Enforces the hard limits no matter what the control loop asks for.

    Belt and braces: the decision engine already respects these, but a bug in the
    planner, a stale telemetry reading or a manual override should never be able to
    push the inverter past its AC rating or the battery past its floor. Every command
    passes through here on its way to the hardware.
    """

    def __init__(
        self,
        inner: BatteryController,
        *,
        max_power_kw: float,
        min_soc_pct: float,
    ) -> None:
        super().__init__()
        self.inner = inner
        self.max_power_kw = max_power_kw
        self.min_soc_pct = min_soc_pct
        self.name = f"safe({inner.name})"
        self.current_soc_pct: float = 100.0
        self.violations: list[str] = []

    def capabilities(self) -> ControllerCapabilities:
        return self.inner.capabilities()

    def observe_soc(self, soc_pct: float) -> None:
        self.current_soc_pct = soc_pct

    def _clamp(self, power_kw: float) -> float:
        if abs(power_kw) > self.max_power_kw:
            self.violations.append(
                f"clamped {power_kw:.2f} kW to the {self.max_power_kw:.1f} kW inverter limit"
            )
            power_kw = self.max_power_kw if power_kw > 0 else -self.max_power_kw
        if power_kw > 0 and self.current_soc_pct <= self.min_soc_pct:
            self.violations.append(
                f"blocked {power_kw:.2f} kW discharge at {self.current_soc_pct:.1f}% SOC "
                f"(floor {self.min_soc_pct:.1f}%)"
            )
            power_kw = 0.0
        return power_kw

    async def set_mode(self, mode: BatteryMode, *, now: datetime, reason: str = "") -> None:
        await self.inner.set_mode(mode, now=now, reason=reason)

    async def set_power(self, power_kw: float, *, now: datetime, reason: str = "") -> None:
        await self.inner.set_power(self._clamp(power_kw), now=now, reason=reason)

    async def set_soc_target(self, soc_pct: float, *, now: datetime, reason: str = "") -> None:
        await self.inner.set_soc_target(max(soc_pct, self.min_soc_pct), now=now, reason=reason)

    async def apply(self, command: ControlCommand) -> None:
        safe = command.model_copy(update={"power_kw": self._clamp(command.power_kw)})
        await self.inner.apply(safe)
        self.last_command = safe
        self.command_log.append(safe)

    async def read_schedule(self) -> list[dict]:
        """Pass-through so the free-window audit can see the real schedule."""
        reader = getattr(self.inner, "read_schedule", None)
        if reader is None:
            raise NotImplementedError(f"{self.inner.name} cannot read its schedule")
        return await reader()

    async def health_check(self) -> bool:
        return await self.inner.health_check()

    async def aclose(self) -> None:
        await self.inner.aclose()
