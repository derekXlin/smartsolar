"""Controller that drives the SimulatedSite physical model."""

from __future__ import annotations

from datetime import datetime

from ..data_providers.simulated import SimulatedSite
from ..models import BatteryMode
from .base import BatteryController, ControllerCapabilities


class SimulatedBatteryController(BatteryController):
    name = "simulated"

    def __init__(self, site: SimulatedSite) -> None:
        super().__init__()
        self.site = site

    def capabilities(self) -> ControllerCapabilities:
        return ControllerCapabilities(
            supports_power_setpoint=True,
            supports_soc_target=False,
            min_command_interval_seconds=0.0,
            max_power_kw=self.site.cfg.inverter.ac_limit_kw,
        )

    async def set_mode(self, mode: BatteryMode, *, now: datetime, reason: str = "") -> None:
        self.site.apply_command(mode, self.site.commanded_kw)

    async def set_power(self, power_kw: float, *, now: datetime, reason: str = "") -> None:
        self.site.apply_command(self.site.mode, power_kw)
