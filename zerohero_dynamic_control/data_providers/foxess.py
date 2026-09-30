"""FoxESS Cloud telemetry.

One call to /op/v0/device/real/query returns every variable we need, which matters
because the daily budget is 1440 calls — batching keeps a 3-hour window at 180 calls
instead of 900.

Sign conventions are normalised here, at the edge, so nothing downstream has to know
that FoxESS reports import and export as two separate non-negative variables while the
rest of this codebase uses one signed grid figure.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..config import AppConfig
from ..foxess_client import FoxESSClient, FoxESSError
from ..models import Telemetry
from .base import ProviderError, TelemetryProvider

log = logging.getLogger(__name__)

# Everything we need, in one request.
VARIABLES = [
    "SoC",                    # %
    "ResidualEnergy",         # battery energy, scaled by 0.01 -> kWh
    "pvPower",                # kW
    "loadsPower",             # kW
    "gridConsumptionPower",   # kW, import, >= 0
    "feedinPower",            # kW, export, >= 0
    "invBatPower",            # kW, signed
    "batDischargePower",      # kW, >= 0  (not present on every firmware)
    "batChargePower",         # kW, >= 0  (not present on every firmware)
]

RESIDUAL_ENERGY_SCALE = 0.01
"""FoxESS reports ResidualEnergy in units of 10 Wh."""


class FoxESSTelemetryProvider(TelemetryProvider):
    name = "foxess"

    def __init__(self, cfg: AppConfig, client: FoxESSClient, serial_number: str) -> None:
        self.cfg = cfg
        self.client = client
        self.sn = serial_number

    async def read(self, now: datetime) -> Telemetry:
        try:
            raw, measured_at = await self.client.real_query_timed(self.sn, VARIABLES)
        except FoxESSError as exc:
            raise ProviderError(f"FoxESS telemetry read failed: {exc}") from exc
        if not raw:
            # Almost always a wrong serial: FoxESS answers errno 0 with an empty
            # payload rather than an error. Name the serial so that is visible.
            raise ProviderError(
                f"FoxESS returned no variables for serial {self.sn!r}. "
                f"The API accepted the request, so the serial is probably wrong — "
                f"run `zerohero foxess-discover` to confirm it."
            )
        return self.to_telemetry(raw, now, measured_at=measured_at)

    def to_telemetry(self, raw: dict[str, float], now: datetime, *,
                     measured_at: datetime | None = None) -> Telemetry:
        """Normalise FoxESS variables into our sign convention.

        Ours: battery_kw > 0 discharging, grid_kw > 0 importing.
        """
        capacity = self.cfg.battery.usable_capacity_kwh

        soc = float(raw.get("SoC", 0.0))
        if "ResidualEnergy" in raw:
            energy = float(raw["ResidualEnergy"]) * RESIDUAL_ENERGY_SCALE
        else:
            # Fall back to SOC x capacity. Less accurate near the extremes, where the
            # BMS reserves margin the SOC figure does not show.
            energy = soc / 100.0 * capacity

        solar = max(0.0, float(raw.get("pvPower", 0.0)))
        load = max(0.0, float(raw.get("loadsPower", 0.0)))
        grid_in = max(0.0, float(raw.get("gridConsumptionPower", 0.0)))
        grid_out = max(0.0, float(raw.get("feedinPower", 0.0)))
        grid = grid_in - grid_out

        # Prefer the explicit pair when the firmware exposes it: invBatPower's sign
        # convention has varied across FoxESS firmware revisions, so an unambiguous
        # reading is worth using when it is available.
        if "batDischargePower" in raw or "batChargePower" in raw:
            battery = float(raw.get("batDischargePower", 0.0)) - float(raw.get("batChargePower", 0.0))
        elif "invBatPower" in raw:
            battery = float(raw["invBatPower"]) * (
                -1.0 if self.cfg.providers.foxess.invert_battery_power_sign else 1.0
            )
        else:
            # Last resort: infer from the balance, grid = load - solar - battery.
            battery = load - solar - grid

        return Telemetry(
            timestamp=now,
            soc_pct=max(0.0, min(100.0, soc)),
            battery_energy_kwh=max(0.0, energy),
            solar_kw=solar,
            load_kw=load,
            battery_kw=battery,
            grid_kw=grid,
            measured_at=measured_at,
        )

    async def aclose(self) -> None:
        await self.client.aclose()


async def discover_serial_number(client: FoxESSClient) -> str:
    """Find the first inverter with a battery. Saves hand-copying the SN."""
    devices = await client.device_list()
    if not devices:
        raise ProviderError("FoxESS account has no devices")
    for dev in devices:
        if dev.get("hasBattery"):
            return str(dev.get("deviceSN") or dev.get("sn"))
    return str(devices[0].get("deviceSN") or devices[0].get("sn"))
