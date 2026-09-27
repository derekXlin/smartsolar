"""Clock abstraction so the control loop is identical in production and simulation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def __init__(self, tz: ZoneInfo) -> None:
        self.tz = tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock:
    """Virtual time. ``speed`` seconds of wall clock per simulated second.

    speed=0 runs the whole evening as fast as the CPU allows, which is what makes it
    practical to sweep hundreds of scenarios in a unit test.
    """

    def __init__(self, start: datetime, speed: float = 0.0) -> None:
        self._now = start
        self.speed = speed

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        if self.speed > 0:
            await asyncio.sleep(seconds * self.speed)
        else:
            await asyncio.sleep(0)  # yield so other tasks can run
