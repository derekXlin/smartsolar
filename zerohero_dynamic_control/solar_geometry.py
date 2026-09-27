"""Sunrise/sunset and clear-sky solar elevation, implemented locally.

Sunset time drives how much residual PV we can still count on after 18:00, and it
moves by nearly two hours between an Australian east-coast summer (~20:05 AEDT) and an east-coast winter
(~16:55 AEST). Hard-coding it would make the controller badly wrong twice a year,
so we compute it from the NOAA solar position equations — accurate to well under a
minute, and with no network dependency that could fail at 17:50.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

_ZENITH_SUNSET_DEG = 90.833  # includes atmospheric refraction and the solar disc radius


def _julian_day(d: date) -> float:
    y, m = d.year, d.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d.day + b - 1524.5


def _solar_declination_and_eot(jd: float) -> tuple[float, float]:
    """Return (declination in radians, equation of time in minutes)."""
    t = (jd - 2451545.0) / 36525.0
    # Geometric mean longitude and anomaly of the sun.
    l0 = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0)
    m = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = math.radians(
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    true_long = l0 + c
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = true_long - math.radians(0.00569) - math.radians(0.00478) * math.sin(omega)
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))

    decl = math.asin(math.sin(eps) * math.sin(app_long))

    y = math.tan(eps / 2.0) ** 2
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l0)
        - 2 * e * math.sin(m)
        + 4 * e * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * e * e * math.sin(2 * m)
    )
    return decl, eot


def _event_utc(day: date, latitude: float, longitude: float, sunrise: bool) -> datetime | None:
    jd = _julian_day(day)
    decl, eot = _solar_declination_and_eot(jd)
    lat = math.radians(latitude)
    cos_ha = (
        math.cos(math.radians(_ZENITH_SUNSET_DEG)) / (math.cos(lat) * math.cos(decl))
        - math.tan(lat) * math.tan(decl)
    )
    if cos_ha > 1 or cos_ha < -1:
        return None  # polar day/night — never happens at the reference site, but be honest about it
    # NOAA convention: sunrise uses +HA, sunset uses -HA in the 720 - 4*(lon + HA) form.
    ha = math.degrees(math.acos(cos_ha))
    if not sunrise:
        ha = -ha
    minutes_utc = 720.0 - 4.0 * (longitude + ha) - eot
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(minutes=minutes_utc)


def sunset(day: date, latitude: float, longitude: float, tz: ZoneInfo) -> datetime:
    """Local sunset. Falls back to 18:00 local if the sun never sets/rises."""
    utc = _event_utc(day, latitude, longitude, sunrise=False)
    if utc is None:
        return datetime.combine(day, datetime.min.time(), tzinfo=tz) + timedelta(hours=18)
    return utc.astimezone(tz)


def sunrise(day: date, latitude: float, longitude: float, tz: ZoneInfo) -> datetime:
    utc = _event_utc(day, latitude, longitude, sunrise=True)
    if utc is None:
        return datetime.combine(day, datetime.min.time(), tzinfo=tz) + timedelta(hours=6)
    return utc.astimezone(tz)


def solar_elevation_deg(when: datetime, latitude: float, longitude: float) -> float:
    """Solar elevation above the horizon, in degrees (negative = below)."""
    utc = when.astimezone(UTC)
    jd = _julian_day(utc.date()) + (utc.hour + utc.minute / 60.0 + utc.second / 3600.0) / 24.0
    decl, eot = _solar_declination_and_eot(jd)
    minutes = utc.hour * 60 + utc.minute + utc.second / 60.0
    true_solar_time = (minutes + eot + 4.0 * longitude) % 1440.0
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


def clear_sky_pv_kw(when: datetime, latitude: float, longitude: float, rating_kw: float,
                    derate: float = 0.78) -> float:
    """Very rough clear-sky AC output for a fixed, roughly north-facing array.

    Used only as a fallback/synthetic profile — a real Solcast or Open-Meteo feed
    should beat it comfortably. The elevation^1.1 shape approximates the combined
    effect of air mass and cosine incidence on a tilted panel.
    """
    elev = solar_elevation_deg(when, latitude, longitude)
    if elev <= 0:
        return 0.0
    intensity = math.sin(math.radians(elev)) ** 1.1
    return max(0.0, min(rating_kw, rating_kw * derate * intensity))
