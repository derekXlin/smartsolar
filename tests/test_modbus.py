"""Local Modbus telemetry: the wire protocol, the FoxESS register map, and failover.

The fake server below speaks real Modbus TCP over a real socket, so these tests
exercise the framing byte for byte rather than a mocked client.
"""

from __future__ import annotations

import asyncio
import struct
from datetime import datetime, timedelta

import pytest

from zerohero_dynamic_control.config import AppConfig, FoxESSModbusConfig
from zerohero_dynamic_control.data_providers.base import (
    FailoverTelemetryProvider,
    ProviderError,
    TelemetryProvider,
)
from zerohero_dynamic_control.data_providers.foxess_modbus import FoxESSModbusTelemetryProvider
from zerohero_dynamic_control.modbus_tcp import (
    ModbusError,
    ModbusExceptionReply,
    ModbusTcpClient,
    combine,
)
from zerohero_dynamic_control.models import Telemetry, soc_to_energy

from .conftest import TZ


class FakeModbusServer:
    """Serves holding registers from a dict; unknown addresses get exception 2."""

    def __init__(self, registers: dict[int, int], unit_id: int = 247):
        self.registers = registers
        self.unit_id = unit_id
        self.requests: list[tuple[int, int]] = []
        self.hang = False
        self.connections = 0
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                tid, proto, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                function, address, count = struct.unpack(">BHH", await reader.readexactly(length - 1))
                self.requests.append((address, count))
                if self.hang:
                    await asyncio.sleep(0.5)
                values = [self.registers.get(a) for a in range(address, address + count)]
                if function != 0x03:
                    pdu = struct.pack(">BB", function | 0x80, 1)
                elif any(v is None for v in values):
                    pdu = struct.pack(">BB", 0x83, 2)
                else:
                    pdu = struct.pack(f">BB{count}H", 0x03, 2 * count, *(v & 0xFFFF for v in values))
                writer.write(struct.pack(">HHHB", tid, proto, len(pdu) + 1, unit) + pdu)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    def client(self, **kw) -> ModbusTcpClient:
        kw.setdefault("timeout", 1.0)
        return ModbusTcpClient("127.0.0.1", self.port, connect_settle_seconds=0,
                               request_gap_seconds=0, **kw)


def put32(regs: dict[int, int], address: int, value: int) -> None:
    """Store a signed 32-bit value the FoxESS way: high word at the lower address."""
    raw = value & 0xFFFFFFFF
    regs[address] = raw >> 16
    regs[address + 1] = raw & 0xFFFF


def h3_smart_registers(
    *, soc=55, remaining_kwh=26.79, load_kw=4.0, battery_kw=2.089, grid_export_kw=-1.914,
    pv_kw=(0.0, 0.0, 0.0, 0.0), second_stack=None,
) -> dict[int, int]:
    """A register image of the H3 Smart at 18:50 on the first live evening."""
    regs = {a: 0 for a in range(39225, 39287)}
    put32(regs, 39225, round(load_kw * 1000))
    put32(regs, 39237, round(battery_kw * 1000))
    for i, kw in enumerate(pv_kw):
        put32(regs, 39279 + 2 * i, round(kw * 1000))
    put32(regs, 38814, round(grid_export_kw * 10000))
    regs[37002], regs[37612], regs[37632] = 1, soc, round(remaining_kwh * 100)
    if second_stack is None:
        regs[37700] = 0
    else:
        regs[37700], regs[38310], regs[38330] = 1, second_stack[0], round(second_stack[1] * 100)
    return regs


NOW = datetime(2026, 9, 27, 18, 50, tzinfo=TZ)


# ------------------------------------------------------------------ protocol
def test_combine_reads_high_word_first_and_signs():
    assert combine([0x0000, 0x0FA0], signed=True) == 4000
    assert combine([0xFFFF, 0xB53C], signed=True) == -19140
    assert combine([0xFFFF], signed=False) == 65535
    assert combine([0xFFFF], signed=True) == -1


@pytest.mark.asyncio
async def test_reads_registers_over_a_real_socket():
    async with FakeModbusServer({100: 1, 101: 2, 102: 0xFFFF}) as srv:
        client = srv.client()
        assert await client.read_holding_registers(100, 3) == [1, 2, 0xFFFF]
        await client.close()


@pytest.mark.asyncio
async def test_exception_reply_is_distinguished_from_a_dead_link():
    """'Illegal address' means the server is fine but the register is not there;
    it must not be confused with the connection failing."""
    async with FakeModbusServer({100: 1}) as srv:
        client = srv.client()
        with pytest.raises(ModbusExceptionReply) as err:
            await client.read_holding_registers(500, 1)
        assert err.value.code == 2
        assert client.connected, "an exception reply leaves the connection usable"
        assert await client.read_holding_registers(100, 1) == [1]
        await client.close()


@pytest.mark.asyncio
async def test_timeout_drops_the_socket_and_the_next_read_reconnects():
    """A late reply left in the stream would be read as the answer to the next
    request, so a timeout must discard the connection."""
    async with FakeModbusServer({100: 7}) as srv:
        client = srv.client(timeout=0.2)
        srv.hang = True
        with pytest.raises(ModbusError):
            await client.read_holding_registers(100, 1)
        assert not client.connected
        srv.hang = False
        assert await client.read_holding_registers(100, 1) == [7]
        assert srv.connections == 2
        await client.close()


@pytest.mark.asyncio
async def test_unreachable_host_is_a_modbus_error():
    client = ModbusTcpClient("127.0.0.1", 1, timeout=0.5, connect_settle_seconds=0)
    with pytest.raises(ModbusError, match="cannot connect"):
        await client.read_holding_registers(0, 1)


# ------------------------------------------------------------- register map
@pytest.mark.asyncio
async def test_h3_smart_registers_map_to_our_sign_conventions():
    """The 18:50 moment: 4 kW load, battery at 2.089 kW, 1.914 kW imported.
    The meter reports export positive; ours is import positive."""
    async with FakeModbusServer(h3_smart_registers()) as srv:
        provider = FoxESSModbusTelemetryProvider(AppConfig(), srv.client())
        tel = await provider.read(NOW)
        await provider.aclose()
    assert tel.soc_pct == 55
    assert tel.battery_energy_kwh == pytest.approx(26.79)
    assert tel.load_kw == pytest.approx(4.0)
    assert tel.battery_kw == pytest.approx(2.089)
    assert tel.grid_kw == pytest.approx(1.914), "importing must read positive"
    assert tel.solar_kw == 0.0
    assert not tel.stale


@pytest.mark.asyncio
async def test_charging_and_exporting_signs():
    regs = h3_smart_registers(battery_kw=-3.0, grid_export_kw=2.5, load_kw=1.5, pv_kw=(3.5, 3.5, 0, 0))
    async with FakeModbusServer(regs) as srv:
        provider = FoxESSModbusTelemetryProvider(AppConfig(), srv.client())
        tel = await provider.read(NOW)
        await provider.aclose()
    assert tel.battery_kw == pytest.approx(-3.0)
    assert tel.grid_kw == pytest.approx(-2.5)
    assert tel.solar_kw == pytest.approx(7.0)
    # The balance the probe checks: solar + battery + grid == load.
    assert tel.solar_kw + tel.battery_kw + tel.grid_kw == pytest.approx(tel.load_kw)


@pytest.mark.asyncio
async def test_two_battery_stacks_are_combined():
    regs = h3_smart_registers(soc=54, remaining_kwh=12.9, second_stack=(56, 13.4))
    async with FakeModbusServer(regs) as srv:
        provider = FoxESSModbusTelemetryProvider(AppConfig(), srv.client())
        tel = await provider.read(NOW)
        await provider.aclose()
    assert tel.soc_pct == pytest.approx(55.0)
    assert tel.battery_energy_kwh == pytest.approx(26.3)


@pytest.mark.asyncio
async def test_a_refused_second_stack_register_means_one_stack():
    regs = h3_smart_registers()
    del regs[37700]
    async with FakeModbusServer(regs) as srv:
        provider = FoxESSModbusTelemetryProvider(AppConfig(), srv.client())
        tel = await provider.read(NOW)
        await provider.aclose()
    assert tel.battery_energy_kwh == pytest.approx(26.79)


@pytest.mark.asyncio
async def test_power_is_read_in_one_block():
    """Load, battery and PV share 39225-39286: one request, not six, every tick."""
    async with FakeModbusServer(h3_smart_registers()) as srv:
        provider = FoxESSModbusTelemetryProvider(AppConfig(), srv.client())
        await provider.read(NOW)
        srv.requests.clear()
        await provider.read(NOW)
        await provider.aclose()
    assert (39225, 62) in srv.requests
    assert not any(37000 <= a < 37100 or 37700 <= a < 37800 for a, _ in srv.requests), \
        "stack detection runs once, not every read"


@pytest.mark.asyncio
async def test_a_dead_link_is_a_provider_error():
    client = ModbusTcpClient("127.0.0.1", 1, timeout=0.5, connect_settle_seconds=0)
    provider = FoxESSModbusTelemetryProvider(AppConfig(), client)
    with pytest.raises(ProviderError, match="Modbus"):
        await provider.read(NOW)


def test_enabling_modbus_without_a_host_is_rejected():
    with pytest.raises(ValueError, match="host"):
        FoxESSModbusConfig(enabled=True)


# ------------------------------------------------------------------ failover
class Scripted(TelemetryProvider):
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.calls = 0

    async def read(self, now: datetime) -> Telemetry:
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} down")
        return Telemetry(timestamp=now, soc_pct=50.0, battery_energy_kwh=soc_to_energy(50.0, 47.0),
                         load_kw=1.0 if self.name == "modbus" else 2.0)


@pytest.mark.asyncio
async def test_failover_uses_the_cloud_sparingly_while_modbus_is_down():
    """Each cloud read spends one of 1440 daily calls; a 10 s loop falling through
    on every tick would spend the day's allowance in four hours."""
    modbus, cloud = Scripted("modbus", fail=True), Scripted("cloud")
    fo = FailoverTelemetryProvider(modbus, cloud, fallback_min_interval=60, primary_retry_seconds=30)
    for s in range(0, 120, 10):
        tel = await fo.read(NOW + timedelta(seconds=s))
        assert tel.load_kw == 2.0 and tel.timestamp == NOW + timedelta(seconds=s)
    assert cloud.calls == 2, "one cloud read per minute, reused in between"
    assert modbus.calls == 4, "the dead primary is retried every 30 s, not every tick"
    assert fo.source == "cloud"


@pytest.mark.asyncio
async def test_failover_returns_to_modbus_when_it_recovers():
    modbus, cloud = Scripted("modbus", fail=True), Scripted("cloud")
    fo = FailoverTelemetryProvider(modbus, cloud, primary_retry_seconds=30)
    await fo.read(NOW)
    modbus.fail = False
    assert (await fo.read(NOW + timedelta(seconds=10))).load_kw == 2.0, "not retried yet"
    tel = await fo.read(NOW + timedelta(seconds=30))
    assert tel.load_kw == 1.0 and fo.source == "modbus"


@pytest.mark.asyncio
async def test_both_sources_down_is_an_error_the_cache_can_handle():
    fo = FailoverTelemetryProvider(Scripted("modbus", fail=True), Scripted("cloud", fail=True))
    with pytest.raises(ProviderError):
        await fo.read(NOW)
