"""Synthetic site profiles for summer, winter and the awkward cases.

Load shapes are calibrated against the customer's own data:
  * Jul-Aug invoice: 42.09 kWh/day total, of which 40.11 kWh was imported free in
    the 11:00-14:00 window, 1.83 kWh at shoulder and 0.15 kWh at peak.
  * September plant report: 1.06 MWh over the month (~35 kWh/day).
That is a large, fairly flat load — consistent with a heat pump and/or EV charging
scheduled into the free window — with a modest evening peak.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..curves import KwAt
from ..solar_geometry import clear_sky_pv_kw

SITE_LAT = -33.7
SITE_LON = 151.0


@dataclass
class Scenario:
    key: str
    label: str
    day: date
    pv_rating_kw: float
    cloud_factor: float
    base_load_kw: float
    evening_peak_kw: float
    start_soc_pct: float
    description: str


SCENARIOS: dict[str, Scenario] = {
    "summer": Scenario(
        key="summer",
        label="Summer evening, clear sky",
        day=date(2026, 1, 15),
        pv_rating_kw=10.0,
        cloud_factor=0.95,
        base_load_kw=1.4,
        evening_peak_kw=2.6,
        start_soc_pct=92.0,
        # Sunset ~20:09 AEDT, so PV is still contributing for two thirds of the
        # window. That is free energy for the house AND competition for the 10 kW
        # inverter port — the case that exposes a naive controller.
        description="Sunset 20:09. Residual PV through most of the window; pack nearly full.",
    ),
    "winter": Scenario(
        key="winter",
        label="Winter evening, clear sky",
        day=date(2026, 6, 21),
        pv_rating_kw=10.0,
        cloud_factor=0.9,
        base_load_kw=2.0,
        evening_peak_kw=4.2,
        start_soc_pct=80.0,
        # Sunset ~16:55 AEST: zero solar for the entire window, and the heaviest
        # evening load of the year. The battery carries all three hours alone.
        description="Sunset 16:55. Zero residual PV, heating load — battery carries the window.",
    ),
    "cloudy": Scenario(
        key="cloudy",
        label="Overcast shoulder-season day",
        day=date(2026, 9, 27),
        pv_rating_kw=10.0,
        cloud_factor=0.15,
        base_load_kw=1.6,
        evening_peak_kw=3.0,
        start_soc_pct=48.0,
        description="Thick cloud all day so the pack never filled. Tests the energy guard.",
    ),
    "high_load": Scenario(
        key="high_load",
        label="High evening load (oven + EV)",
        day=date(2026, 3, 10),
        pv_rating_kw=10.0,
        cloud_factor=0.8,
        base_load_kw=2.2,
        evening_peak_kw=8.5,
        start_soc_pct=85.0,
        # 8.5 kW of house load leaves only 1.5 kW of export headroom under the 10 kW
        # AC limit — equation (5) in decision_engine, and the reason the planner
        # cannot simply divide the export budget by three hours.
        description="8.5 kW evening load. Export headroom collapses against the 10 kW AC limit.",
    ),
    "low_soc": Scenario(
        key="low_soc",
        label="Depleted battery, unwinnable window",
        day=date(2026, 6, 21),
        pv_rating_kw=10.0,
        cloud_factor=0.2,
        base_load_kw=2.5,
        evening_peak_kw=4.5,
        start_soc_pct=22.0,
        description="Not enough charge to hold three hours. Should abandon the credit cleanly.",
    ),
}


def solar_curve(scenario: Scenario, tz: ZoneInfo) -> KwAt:
    def _solar(when: datetime) -> float:
        base = clear_sky_pv_kw(when, SITE_LAT, SITE_LON, scenario.pv_rating_kw)
        # A slow sinusoidal wobble on top of the cloud factor, so the controller has
        # to cope with PV that moves rather than a smooth analytic curve.
        wobble = 1.0 + 0.12 * math.sin(when.hour * 2.3 + when.minute / 7.0)
        return max(0.0, base * scenario.cloud_factor * wobble)

    return _solar


def load_curve(scenario: Scenario) -> KwAt:
    """Flat base load with a broad evening hump peaking around 19:00."""

    def _load(when: datetime) -> float:
        hour = when.hour + when.minute / 60.0
        # Gaussian hump centred at 19:00 with a ~2 h spread.
        hump = math.exp(-((hour - 19.0) ** 2) / (2 * 1.8**2))
        extra = (scenario.evening_peak_kw - scenario.base_load_kw) * hump
        # Small high-frequency jitter for appliance cycling.
        jitter = 0.12 * math.sin(hour * 37.0)
        return max(0.1, scenario.base_load_kw + extra + jitter)

    return _load
