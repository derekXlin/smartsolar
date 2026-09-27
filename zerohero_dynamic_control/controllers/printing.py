"""Placeholder controller: prints what it would do.

This is the default, so the project is runnable the moment it is cloned, before any
inverter credentials exist. Swap `controller.type` in config.yaml once the battery
brand is known.
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console

from ..models import BatteryMode
from .base import BatteryController, ControllerCapabilities

console = Console()


class PrintingBatteryController(BatteryController):
    name = "printing"

    def __init__(self, quiet: bool = False) -> None:
        super().__init__()
        self.quiet = quiet
        self.mode: BatteryMode = BatteryMode.SELF_CONSUMPTION
        self.power_kw: float = 0.0

    def capabilities(self) -> ControllerCapabilities:
        return ControllerCapabilities(
            supports_power_setpoint=True,
            supports_soc_target=True,
            min_command_interval_seconds=0.0,
        )

    def _say(self, text: str) -> None:
        if not self.quiet:
            console.print(text)

    async def set_mode(self, mode: BatteryMode, *, now: datetime, reason: str = "") -> None:
        self.mode = mode
        self._say(f"[bold cyan]{now:%H:%M:%S}[/] MODE  -> [bold]{mode.value}[/]"
                  + (f"  [dim]({reason})[/]" if reason else ""))

    async def set_power(self, power_kw: float, *, now: datetime, reason: str = "") -> None:
        self.power_kw = power_kw
        verb = "discharge" if power_kw >= 0 else "charge"
        self._say(f"[cyan]{now:%H:%M:%S}[/] POWER -> {abs(power_kw):5.2f} kW {verb}"
                  + (f"  [dim]({reason})[/]" if reason else ""))

    def attach_schedule_reader(self, reader) -> None:
        """Let shadow mode audit the real scheduler while commanding nothing.

        With controller.type=printing and providers.battery=foxess you get real
        telemetry and a real configuration audit, but zero writes to the inverter.
        """
        self._schedule_reader = reader

    async def read_schedule(self) -> list[dict]:
        reader = getattr(self, "_schedule_reader", None)
        if reader is None:
            raise NotImplementedError("no schedule reader attached")
        return await reader()

    async def set_soc_target(self, soc_pct: float, *, now: datetime, reason: str = "") -> None:
        self._say(f"[cyan]{now:%H:%M:%S}[/] SOC   -> stop at {soc_pct:.1f}%")
