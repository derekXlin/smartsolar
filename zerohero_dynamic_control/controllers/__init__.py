"""Battery controller implementations."""

from __future__ import annotations

from ..config import AppConfig
from .base import BatteryController, ControllerCapabilities, ControllerError, SafetyWrapper
from .printing import PrintingBatteryController

__all__ = [
    "BatteryController",
    "ControllerCapabilities",
    "ControllerError",
    "SafetyWrapper",
    "PrintingBatteryController",
    "build_controller",
]


def build_controller(cfg: AppConfig) -> BatteryController:
    """Instantiate the configured controller, wrapped in the safety limiter."""
    kind = cfg.controller.type
    if kind == "printing":
        inner: BatteryController = PrintingBatteryController()
    elif kind == "homeassistant":
        from .homeassistant import HomeAssistantController

        inner = HomeAssistantController(cfg.controller.options)
    elif kind == "foxess":
        from ..foxess_client import CallBudget, FoxESSClient
        from .foxess import FoxESSController

        fox = cfg.providers.foxess
        if not fox.serial_number:
            raise ControllerError(
                "providers.foxess.serial_number is required for the FoxESS controller; "
                "run `zerohero foxess-discover` to find it"
            )
        client = FoxESSClient(
            fox.api_key or "",
            base_url=fox.base_url,
            timezone=cfg.site.timezone,
            budget=CallBudget(daily_limit=fox.daily_call_limit, reserve=fox.call_reserve),
        )
        inner = FoxESSController(
            client,
            fox.serial_number,
            window_start=cfg.plan.credit_window_start,
            window_end=cfg.plan.credit_window_end,
            min_soc_on_grid_pct=int(cfg.battery.emergency_floor_soc_pct),
            # Deadman floor: where an unattended inverter stops if we die mid-window.
            fd_soc_pct=int(cfg.battery.min_reserve_soc_pct),
            max_power_kw=cfg.inverter.ac_limit_kw,
            preserve_baseline=fox.preserve_existing_schedule,
        )
    elif kind == "tesla_fleet":
        from .tesla_fleet import TeslaFleetController

        inner = TeslaFleetController(cfg.controller.options)
    elif kind == "simulated":
        raise ControllerError("the simulated controller is built by the simulation harness")
    else:
        raise ControllerError(f"unknown controller type {kind!r}")

    return SafetyWrapper(
        inner,
        max_power_kw=cfg.inverter.ac_limit_kw,
        min_soc_pct=cfg.battery.emergency_floor_soc_pct,
    )
