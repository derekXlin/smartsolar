"""Tesla Powerwall controller skeleton (Fleet API / pypowerwall / Netzero).

NOT wired to credentials — it documents the mapping and raises clearly if used
before it is finished, so nobody mistakes it for a working integration.

The important design point for Powerwall specifically: the Fleet API does NOT expose
a continuous battery power setpoint. You get:

    * operation mode            "self_consumption" | "autonomous" | "backup"
    * backup_reserve_percent    0-100
    * (TOU) tariff / peak-price schedule

So the way to force export 18:00-21:00 is to run "autonomous" (Time-Based Control)
with a tariff whose peak price during 18:00-21:00 is far above the off-peak buy
price, which makes the Powerwall's own optimiser choose to sell. You then steer the
*depth* of the discharge with backup_reserve_percent rather than with kW.

That is why ``capabilities()`` reports supports_power_setpoint=False and
supports_soc_target=True: the control loop reads those flags and automatically
switches from fine power modulation to SOC-target control. The trade-off is coarser
control of the import margin, so the loop widens its safety margin in that mode.

Third-party options that DO give direct control:
    * pypowerwall in local mode (the internal /api/config endpoints)
    * Netzero (netzero.energy) "export now" commands
Both can be dropped in behind this same class.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import BatteryMode
from .base import BatteryController, ControllerCapabilities, ControllerError

MODE_MAP = {
    BatteryMode.SELF_CONSUMPTION: "self_consumption",
    BatteryMode.FORCE_EXPORT: "autonomous",   # + a peak-price TOU window 18:00-21:00
    BatteryMode.FORCE_CHARGE: "autonomous",   # + a cheap/negative-price window 11:00-14:00
    BatteryMode.HOLD: "backup",
    BatteryMode.BACKUP: "backup",
}


class TeslaFleetController(BatteryController):
    name = "tesla_fleet"

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__()
        self.energy_site_id = options.get("energy_site_id")
        self.refresh_token = options.get("refresh_token")
        self.options = options

    def capabilities(self) -> ControllerCapabilities:
        # See the module docstring: depth-of-discharge control only.
        return ControllerCapabilities(
            supports_power_setpoint=False,
            supports_soc_target=True,
            min_command_interval_seconds=60.0,
        )

    async def set_mode(self, mode: BatteryMode, *, now: datetime, reason: str = "") -> None:
        raise ControllerError(
            "TeslaFleetController is a documented skeleton. Implement the Fleet API calls "
            f"(POST /api/1/energy_sites/{self.energy_site_id}/operation with "
            f"default_real_mode={MODE_MAP[mode]!r}) before enabling controller.type=tesla_fleet."
        )

    async def set_power(self, power_kw: float, *, now: datetime, reason: str = "") -> None:
        raise ControllerError("Powerwall has no direct power setpoint; use set_soc_target")

    async def set_soc_target(self, soc_pct: float, *, now: datetime, reason: str = "") -> None:
        raise ControllerError(
            "Implement POST /api/1/energy_sites/{id}/backup with "
            f"backup_reserve_percent={soc_pct:.0f}"
        )
