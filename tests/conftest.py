from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.decision_engine import DecisionEngine
from zerohero_dynamic_control.models import Telemetry, soc_to_energy
from zerohero_dynamic_control.solar_geometry import clear_sky_pv_kw

TZ = ZoneInfo("Australia/Sydney")
LAT, LON = -33.7, 151.0


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig()


@pytest.fixture
def engine(cfg: AppConfig) -> DecisionEngine:
    return DecisionEngine(cfg)


def make_telemetry(cfg: AppConfig, when: datetime, soc_pct: float, load_kw: float, solar_kw: float = 0.0) -> Telemetry:
    return Telemetry(
        timestamp=when,
        soc_pct=soc_pct,
        battery_energy_kwh=soc_to_energy(soc_pct, cfg.battery.usable_capacity_kwh),
        solar_kw=solar_kw,
        load_kw=load_kw,
        grid_kw=load_kw - solar_kw,
    )


def solar_fn(pv_rating_kw: float, cloud: float = 1.0):
    def _f(when: datetime) -> float:
        return clear_sky_pv_kw(when, LAT, LON, pv_rating_kw) * cloud

    return _f


def flat(value: float):
    def _f(_when: datetime) -> float:
        return value

    return _f


def at(year: int, month: int, day: int, hour: int = 17, minute: int = 50) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)
