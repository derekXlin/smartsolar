"""The tariff model must reproduce the customer's actual GloBird invoice."""

from __future__ import annotations

from datetime import time

import pytest

from zerohero_dynamic_control.economics import best_case_daily_pnl, project_daily_pnl
from zerohero_dynamic_control.tariff import Tariff, marginal_value_of_stored_energy


@pytest.fixture
def tariff() -> Tariff:
    return Tariff()


def test_reproduces_the_reference_invoice(tariff):
    """A real ZEROHERO invoice, 15-Jul-2026 to 11-Aug-2026, total $29.78 inc GST.

    If this test fails, the rates in tariff.py have drifted from the real plan and
    every dollar figure the engine produces is wrong.
    """
    total = (
        28 * tariff.daily_supply_charge_aud
        + 4.18 * tariff.import_rate(time(19, 0))      # Peak
        + 1123.10 * tariff.import_rate(time(12, 0))   # Offpeak (free)
        + 51.30 * tariff.import_rate(time(2, 0))      # Shoulder
        - 126.06 * tariff.super_export_topup_aud_per_kwh
        - 128.86 * 0.02                                # FiT 4pm-11pm
        - 10.18 * 0.0                                  # FiT 11pm-4pm
        - 25.0 * tariff.zerohero_credit_aud            # credit earned on 25 of 28 days
    )
    assert total == pytest.approx(29.78, abs=0.01)


@pytest.mark.parametrize(
    "hour,expected",
    [(9, 0.407), (12, 0.0), (13, 0.0), (15, 0.407), (17, 0.528), (19, 0.528), (22, 0.528), (23, 0.407), (2, 0.407)],
)
def test_import_rates_by_hour(tariff, hour, expected):
    assert tariff.import_rate(time(hour, 0)) == pytest.approx(expected)


@pytest.mark.parametrize(
    "hour,expected",
    [
        (12, 0.00),  # daytime export earns absolutely nothing on this plan
        (17, 0.02),  # evening FiT, before the Super Export window opens
        (19, 0.10),  # FiT + Super Export top up — the only valuable export hour
        (22, 0.02),  # after 21:00 the top-up stops
        (2, 0.00),
    ],
)
def test_export_rates_by_hour(tariff, hour, expected):
    assert tariff.export_rate(time(hour, 0)) == pytest.approx(expected)


def test_in_window_export_is_five_times_daytime_export(tariff):
    """The core reason the controller exists: timing export is worth more than
    generating more of it."""
    assert tariff.export_rate(time(19, 0)) == pytest.approx(0.10)
    assert tariff.export_rate(time(12, 0)) == pytest.approx(0.0)


def test_blended_overnight_rate(tariff):
    """21:00-23:00 is peak, 23:00-11:00 is shoulder."""
    blended = tariff.blended_import_rate(time(21, 0), 14.0)
    assert blended == pytest.approx((2 * 0.528 + 12 * 0.407) / 14, abs=0.001)


def test_retained_energy_beats_exported_energy(tariff):
    """Keeping a kWh is worth ~4x selling it — as long as it displaces import."""
    assert tariff.blended_import_rate(time(21, 0), 14.0) > 4 * tariff.export_rate(time(19, 0))


def test_best_case_day_is_cash_positive(tariff):
    """A compliant day that fills the 15 kWh cap should more than cover supply."""
    best = best_case_daily_pnl(tariff)
    assert best.is_cash_positive
    assert best.net_aud == pytest.approx(1.584 - 1.0 - 15 * 0.10, abs=0.01)


def test_missing_the_credit_costs_a_dollar(tariff):
    with_credit = project_daily_pnl(tariff, in_window_export_kwh=8.0, credit_secured=True)
    without = project_daily_pnl(tariff, in_window_export_kwh=8.0, credit_secured=False)
    assert without.net_aud - with_credit.net_aud == pytest.approx(1.0, abs=0.001)


def test_super_export_cap_limits_the_topup(tariff):
    """Exporting 25 kWh earns the top-up on only the first 15."""
    pnl = project_daily_pnl(tariff, in_window_export_kwh=25.0)
    assert pnl.super_export_topup_aud == pytest.approx(15 * 0.08, abs=0.001)


# ------------------------------------------------- marginal value of storage
def test_full_pack_with_sunny_morning_is_mostly_stranded():
    """Pack nearly full, big solar morning, free window can refill anyway →
    the surplus cannot displace paid import, so it should be sold."""
    v = marginal_value_of_stored_energy(
        energy_at_window_close_kwh=44.0,
        usable_capacity_kwh=47.0,
        overnight_consumption_kwh=10.0,
        morning_solar_to_battery_kwh=15.0,
        free_window_recharge_kwh=28.5,
        reserve_floor_kwh=11.75,
        blended_import_rate_aud_per_kwh=0.424,
        export_rate_aud_per_kwh=0.10,
    )
    assert v.stranded_kwh > 0
    assert v.should_export_stranded


def test_empty_pack_with_dull_morning_is_not_stranded():
    """Low charge and no morning sun → every kWh will displace paid import."""
    v = marginal_value_of_stored_energy(
        energy_at_window_close_kwh=18.0,
        usable_capacity_kwh=47.0,
        overnight_consumption_kwh=16.0,
        morning_solar_to_battery_kwh=1.0,
        free_window_recharge_kwh=28.5,
        reserve_floor_kwh=11.75,
        blended_import_rate_aud_per_kwh=0.424,
        export_rate_aud_per_kwh=0.10,
    )
    assert v.stranded_kwh == pytest.approx(0.0, abs=1e-6)
    assert v.displaceable_kwh > 0
