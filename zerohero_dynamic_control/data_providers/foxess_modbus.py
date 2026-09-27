"""FoxESS telemetry read locally over Modbus TCP, bypassing the cloud.

WHY
---
The cloud's real-time feed is a snapshot the logger uploads roughly every five
minutes. The ZeroHero budget is 30 Wh per hour, which a 2 kW kettle spends in
under a minute, so a five-minute-old picture of the house cannot protect it: on
the first live evening a 4 kW load step at 18:50 was invisible until the next
upload, and the hour was lost. Read locally, the same registers are seconds old.

It also takes telemetry off the 1440/day cloud quota entirely, leaving the whole
allowance for scheduler writes.

WHERE THE REGISTERS COME FROM
-----------------------------
The H3 Smart (product type H3-G2) register map, holding registers read with
function 0x03, as used by the foxess_modbus Home Assistant integration's
``Inv.H3_SMART`` profile. Every 32-bit value is two registers with the HIGH word
at the LOWER address. The built-in WL-H3-G2 logger serves them on port 502,
unit id 247. An RS485-to-TCP adapter on the inverter's COM port serves the same
map.

Signs are normalised here, at the edge, as the cloud provider does:
ours is battery_kw > 0 discharging, grid_kw > 0 importing.

`zerohero modbus-probe` reads these side by side with the cloud feed. Run it
before enabling this provider: a wrong scale or sign here would steer the
battery on fiction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..config import AppConfig
from ..modbus_tcp import ModbusError, ModbusExceptionReply, ModbusTcpClient, combine
from ..models import Telemetry
from .base import ProviderError, TelemetryProvider

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Register:
    address: int          # the lowest address; for 32-bit values, the HIGH word
    words: int            # 1 or 2
    scale: float
    signed: bool


# Inverter-level power, one contiguous block read.
LOAD_POWER = Register(39225, 2, 0.001, True)         # kW, house load
BATTERY_POWER = Register(39237, 2, 0.001, True)      # kW, +discharge / -charge
PV_POWER = [Register(a, 2, 0.001, True) for a in (39279, 39281, 39283, 39285)]  # kW, PV1..PV4
GRID_METER = Register(38814, 2, 0.0001, True)        # kW, +export / -import (0.1 W units)

# Per battery stack (BMS). The H3 Smart supports two; each is read separately
# because foxess_modbus found these ranges must be read one register at a time.
@dataclass(frozen=True)
class Stack:
    connect_state: Register    # 0 initial, 1 OK, 2 NG
    soc: Register              # %
    remaining: Register        # kWh, 0.01 units


STACKS = [
    Stack(Register(37002, 1, 1, False), Register(37612, 1, 1, False), Register(37632, 1, 0.01, False)),
    Stack(Register(37700, 1, 1, False), Register(38310, 1, 1, False), Register(38330, 1, 0.01, False)),
]

POWER_BLOCK = (39225, 39286 - 39225 + 1)
"""Load, battery and PV1-4 all sit in 39225-39286: one read instead of six."""


def _decode(block_start: int, block: list[int], reg: Register) -> float:
    i = reg.address - block_start
    return combine(block[i:i + reg.words], signed=reg.signed) * reg.scale


class FoxESSModbusTelemetryProvider(TelemetryProvider):
    name = "foxess-modbus"

    def __init__(self, cfg: AppConfig, client: ModbusTcpClient) -> None:
        self.cfg = cfg
        self.client = client
        self._stacks: list[Stack] | None = None

    async def _read(self, reg: Register) -> float:
        words = await self.client.read_holding_registers(reg.address, reg.words)
        return combine(words, signed=reg.signed) * reg.scale

    async def _connected_stacks(self) -> list[Stack]:
        """Which battery stacks exist. Asked once; a second stack does not appear mid-evening."""
        if self._stacks is None:
            found = []
            for i, stack in enumerate(STACKS, start=1):
                try:
                    state = int(await self._read(stack.connect_state))
                except ModbusExceptionReply as exc:
                    # Refused, not unreachable: this firmware has no such stack.
                    # A dropped connection must NOT land here, or a network blip
                    # would permanently hide half the battery.
                    log.info("battery stack %d: connect state refused (%s) — treating as absent", i, exc)
                    continue
                # foxess_modbus: 0 initial, 1 OK, 2 NG; some firmware reports other
                # values for a present stack, so only 0 and 2 mean "not there".
                if state not in (0, 2):
                    found.append(stack)
                log.info("battery stack %d: connect state %d%s", i, state, "" if state not in (0, 2) else " (absent)")
            if not found:
                raise ProviderError("Modbus: no battery stack reports connected (BMS state 0/2 on every stack)")
            self._stacks = found
        return self._stacks

    async def read_raw(self) -> dict[str, float]:
        """Every value, decoded but not yet mapped to our conventions. Used by the probe."""
        start, count = POWER_BLOCK
        block = await self.client.read_holding_registers(start, count)
        grid_export = await self._read(GRID_METER)
        out = {
            "load_kw": _decode(start, block, LOAD_POWER),
            "battery_kw": _decode(start, block, BATTERY_POWER),
            "grid_export_kw": grid_export,
        }
        for n, reg in enumerate(PV_POWER, start=1):
            out[f"pv{n}_kw"] = _decode(start, block, reg)
        stacks = await self._connected_stacks()
        for n, stack in enumerate(stacks, start=1):
            out[f"stack{n}_soc_pct"] = await self._read(stack.soc)
            out[f"stack{n}_remaining_kwh"] = await self._read(stack.remaining)
        out["stacks"] = float(len(stacks))
        return out

    def to_telemetry(self, raw: dict[str, float], now: datetime) -> Telemetry:
        stacks = int(raw["stacks"])
        socs = [raw[f"stack{n}_soc_pct"] for n in range(1, stacks + 1)]
        energy = sum(raw[f"stack{n}_remaining_kwh"] for n in range(1, stacks + 1))
        # Equal-sized stacks in practice; the cloud's single SoC figure is their mean.
        soc = sum(socs) / len(socs)
        # A PV string that is not fitted reads zero; one reading slightly negative
        # at night is sensor offset, not generation.
        solar = sum(max(0.0, raw[f"pv{n}_kw"]) for n in range(1, len(PV_POWER) + 1))
        return Telemetry(
            timestamp=now,
            soc_pct=max(0.0, min(100.0, soc)),
            battery_energy_kwh=max(0.0, energy),
            solar_kw=solar,
            load_kw=max(0.0, raw["load_kw"]),
            battery_kw=raw["battery_kw"],
            # The meter reports export positive; ours is import positive.
            grid_kw=-raw["grid_export_kw"],
        )

    async def read(self, now: datetime) -> Telemetry:
        try:
            raw = await self.read_raw()
        except ModbusError as exc:
            raise ProviderError(f"FoxESS Modbus read failed: {exc}") from exc
        return self.to_telemetry(raw, now)

    async def aclose(self) -> None:
        await self.client.close()
