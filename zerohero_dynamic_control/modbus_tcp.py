"""A minimal async Modbus TCP client: read holding registers, nothing else.

Why not pymodbus: we need exactly one function code, and pymodbus has renamed
the unit-id argument twice across 3.x minor releases (``unit`` -> ``slave`` ->
``device_id``). A pin would hold, but an unpinned rebuild of the image would
break telemetry at the worst possible moment. Function 0x03 is thirty lines of
framing, so owning it is cheaper than tracking someone else's API.

Framing (Modbus Application Protocol over TCP):

    MBAP header  transaction id (2) | protocol id = 0 (2) | length (2) | unit id (1)
    request PDU  0x03 | start address (2) | register count (2)
    response PDU 0x03 | byte count (1) | registers (2 each, big-endian)
    exception    0x83 | exception code (1)

One request is in flight at a time. The FoxESS logger is a small embedded
server and answers one client request at a time anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct

log = logging.getLogger(__name__)

READ_HOLDING_REGISTERS = 0x03
MAX_REGISTERS_PER_READ = 125
"""The protocol limit for one function-0x03 request."""

EXCEPTION_NAMES = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "server device failure",
    6: "server device busy",
    10: "gateway path unavailable",
    11: "gateway target failed to respond",
}


class ModbusError(RuntimeError):
    """Any failure to get a valid answer: connect, timeout, framing or exception reply."""


class ModbusExceptionReply(ModbusError):
    """The server answered, but refused the request (e.g. illegal data address)."""

    def __init__(self, code: int, address: int, count: int) -> None:
        self.code = code
        super().__init__(
            f"Modbus exception {code} ({EXCEPTION_NAMES.get(code, 'unknown')}) "
            f"reading {count} register(s) at {address}"
        )


class ModbusTcpClient:
    def __init__(
        self,
        host: str,
        port: int = 502,
        *,
        unit_id: int = 247,
        timeout: float = 3.0,
        connect_settle_seconds: float = 1.0,
        request_gap_seconds: float = 0.03,
    ) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        # The FoxESS logger drops the first request if it arrives straight after
        # the TCP handshake, and needs a short breather between requests. Both
        # figures come from the foxess_modbus integration's LAN adapter.
        self.connect_settle_seconds = connect_settle_seconds
        self.request_gap_seconds = request_gap_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._tid = 0

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except (TimeoutError, OSError) as exc:
            raise ModbusError(f"cannot connect to {self.host}:{self.port}: {exc!r}") from exc
        log.info("Modbus connected to %s:%d (unit %d)", self.host, self.port, self.unit_id)
        if self.connect_settle_seconds:
            await asyncio.sleep(self.connect_settle_seconds)

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        """Return ``count`` raw 16-bit registers starting at ``address``."""
        if not 1 <= count <= MAX_REGISTERS_PER_READ:
            raise ValueError(f"count must be 1..{MAX_REGISTERS_PER_READ}, got {count}")
        async with self._lock:
            if not self.connected:
                await self._connect()
            try:
                return await asyncio.wait_for(self._transact(address, count), self.timeout)
            except ModbusExceptionReply:
                raise  # the connection itself is fine
            except (TimeoutError, OSError, asyncio.IncompleteReadError, ModbusError) as exc:
                # Drop the socket: after a timeout the stream may still hold a late
                # reply, which would be misread as the answer to the next request.
                await self.close()
                if isinstance(exc, ModbusError):
                    raise
                raise ModbusError(
                    f"no valid reply from {self.host}:{self.port} for {count} register(s) "
                    f"at {address}: {exc!r}"
                ) from exc
            finally:
                if self.request_gap_seconds:
                    await asyncio.sleep(self.request_gap_seconds)

    async def _transact(self, address: int, count: int) -> list[int]:
        assert self._reader is not None and self._writer is not None
        self._tid = (self._tid + 1) & 0xFFFF
        pdu = struct.pack(">BHH", READ_HOLDING_REGISTERS, address, count)
        self._writer.write(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit_id) + pdu)
        await self._writer.drain()

        tid, proto, length, unit = struct.unpack(">HHHB", await self._reader.readexactly(7))
        body = await self._reader.readexactly(length - 1)
        if tid != self._tid or proto != 0:
            raise ModbusError(f"mismatched reply (tid {tid} != {self._tid}, protocol {proto})")
        if unit != self.unit_id:
            raise ModbusError(f"reply from unit {unit}, expected {self.unit_id}")
        function = body[0]
        if function == READ_HOLDING_REGISTERS | 0x80:
            raise ModbusExceptionReply(body[1], address, count)
        if function != READ_HOLDING_REGISTERS:
            raise ModbusError(f"unexpected function code 0x{function:02x} in reply")
        byte_count = body[1]
        if byte_count != 2 * count or len(body) != 2 + byte_count:
            raise ModbusError(f"expected {2 * count} data bytes, got {byte_count}")
        return list(struct.unpack(f">{count}H", body[2:]))


def combine(words: list[int], *, signed: bool) -> int:
    """Join registers stored high word first (FoxESS order) into one integer."""
    value = 0
    for w in words:
        value = (value << 16) | (w & 0xFFFF)
    if signed:
        sign_bit = 1 << (16 * len(words) - 1)
        value = (value & (sign_bit - 1)) - (value & sign_bit)
    return value
