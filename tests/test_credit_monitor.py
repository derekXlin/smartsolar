"""The 0.03 kWh/hour rule is per clock hour, and that detail decides the $1."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from zerohero_dynamic_control.credit_monitor import CreditMonitor

TZ = ZoneInfo("Australia/Sydney")
START = datetime(2026, 1, 15, 18, 0, tzinfo=TZ)
END = START + timedelta(hours=3)


def feed(monitor: CreditMonitor, grid_kw_at, minutes: int = 180, step: int = 1) -> None:
    t = START
    for _ in range(0, minutes, step):
        monitor.observe(t, grid_kw_at(t))
        t += timedelta(minutes=step)


def test_three_hour_buckets_created():
    m = CreditMonitor(START, END)
    assert sorted(b.hour_start.hour for b in m.buckets.values()) == [18, 19, 20]


def test_clean_export_secures_the_credit():
    m = CreditMonitor(START, END)
    feed(m, lambda t: -2.5)
    assert m.credit_secured
    assert m.total_import_kwh == pytest.approx(0.0)


def test_tiny_sustained_import_stays_under_the_limit():
    """100 W for 10 minutes = 0.0167 kWh — under the 0.03 kWh allowance."""
    m = CreditMonitor(START, END)
    feed(m, lambda t: 0.1 if t.hour == 19 and t.minute < 10 else -2.0)
    assert m.credit_secured


def test_short_high_spike_breaches_a_single_hour():
    """1.5 kW for 2 minutes = 0.05 kWh — one appliance start loses the whole day."""
    m = CreditMonitor(START, END)
    spike = datetime(2026, 1, 15, 20, 10, tzinfo=TZ)
    feed(m, lambda t: 1.5 if spike <= t < spike + timedelta(minutes=2) else -2.0)
    assert not m.credit_secured
    breached = m.breached_hours()
    assert len(breached) == 1 and breached[0].hour_start.hour == 20


def test_a_breach_in_one_hour_cannot_be_made_up_in_another():
    """This is why the rule is per-hour: exporting hard later does not help."""
    m = CreditMonitor(START, END)
    feed(m, lambda t: 2.0 if t.hour == 18 and t.minute < 5 else -9.0)
    assert not m.credit_secured


def test_energy_splits_correctly_across_an_hour_boundary():
    m = CreditMonitor(START, END)
    # 1.2 kW held from 18:59 to 19:01 = 0.04 kWh, split evenly either side of 19:00.
    m.observe(datetime(2026, 1, 15, 18, 59, tzinfo=TZ), 1.2)
    m.observe(datetime(2026, 1, 15, 19, 1, tzinfo=TZ), 1.2)
    h18 = m.buckets[datetime(2026, 1, 15, 18, 0, tzinfo=TZ)]
    h19 = m.buckets[datetime(2026, 1, 15, 19, 0, tzinfo=TZ)]
    assert h18.imported_kwh == pytest.approx(0.02, abs=1e-6)
    assert h19.imported_kwh == pytest.approx(0.02, abs=1e-6)


def test_headroom_fraction_falls_as_the_allowance_is_consumed():
    m = CreditMonitor(START, END)
    at = datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    assert m.headroom_fraction(at) == pytest.approx(1.0)
    m.observe(datetime(2026, 1, 15, 18, 0, tzinfo=TZ), 0.9)
    m.observe(datetime(2026, 1, 15, 18, 1, tzinfo=TZ), 0.9)  # 0.015 kWh = half the budget
    assert m.headroom_fraction(at) == pytest.approx(0.5, abs=0.02)


def test_absurd_time_gaps_are_ignored_not_integrated():
    """A process restart must not fabricate hours of phantom import."""
    m = CreditMonitor(START, END)
    m.observe(START, 3.0)
    m.observe(START + timedelta(hours=2), 3.0)
    assert m.total_import_kwh == pytest.approx(0.0)


def test_an_unwatched_window_is_not_secured():
    """Seen live: a restart closed out a window it never sampled, and the monitor's
    pre-built empty buckets reported a clean pass. No samples is no evidence."""
    m = CreditMonitor(START, END)
    assert m.breach_free
    assert not m.credit_verified
    assert not m.credit_secured
    assert "UNVERIFIED" in m.report()


def test_late_takeover_leaves_the_first_hour_unverified():
    """Taking over at 18:17 leaves 17 minutes of 18:00 unwatched — enough for a
    kettle to have lost the hour without us knowing."""
    m = CreditMonitor(START, END)
    t = START + timedelta(minutes=17)
    while t < END:
        m.observe(t, -1.0)
        t += timedelta(minutes=1)
    assert [b.hour_start.hour for b in m.unverified_hours()] == [18]
    assert m.breach_free and not m.credit_secured


def test_a_short_restart_gap_still_counts_as_watched():
    """A redeploy costs a minute or two; that must not void an otherwise clean hour."""
    m = CreditMonitor(START, END)
    t = START
    while t < END:
        m.observe(t, -1.0)
        t += timedelta(minutes=3 if t.hour == 19 and t.minute == 30 else 1)
    assert m.credit_secured


def test_a_long_gap_is_neither_integrated_nor_watched():
    m = CreditMonitor(START, END)
    m.observe(START, 3.0)
    m.observe(START + timedelta(minutes=20), 3.0)
    assert m.total_import_kwh == pytest.approx(0.0)
    assert m.buckets[START].observed_minutes == pytest.approx(0.0)


def _outcome(wh_by_hour, observed=60.0):
    from zerohero_dynamic_control.models import DailyOutcome, HourImport

    hours = [HourImport(hour_start=START + timedelta(hours=i), imported_kwh=wh / 1000,
                        observed_minutes=observed) for i, wh in enumerate(wh_by_hour)]
    secured = all(not h.breached for h in hours)
    verified = all(h.verified for h in hours)
    return DailyOutcome(date="2026-09-28", hourly_import=hours,
                        credit_secured=secured and verified, credit_verified=verified)


def test_an_estimate_just_over_the_limit_defers_to_the_bill():
    """28 Sep: the controller estimated 13 / 61 / 23 Wh from five-minute cloud
    readings, and GloBird paid the credit. Calling that a miss was wrong."""
    from zerohero_dynamic_control.ledger import verdict_of

    assert verdict_of(_outcome([13.3, 60.8, 22.6])) == "CHECK BILL"


def test_an_estimate_far_over_the_limit_is_a_miss():
    """27 Sep: 165 Wh estimated in the 18:00 hour, and the credit was lost."""
    from zerohero_dynamic_control.ledger import verdict_of

    assert verdict_of(_outcome([164.7, 0.0, 0.0])) == "MISSED"
    assert verdict_of(_outcome([5.0, 4.0, 3.0])) == "SECURED"
    assert verdict_of(_outcome([5.0, 4.0, 3.0], observed=30.0)) == "UNVERIFIED"


def test_report_labels_uncertain_hours_as_estimates():
    m = CreditMonitor(START, END)
    m.buckets[START].imported_kwh = 0.061
    m.buckets[START + timedelta(hours=1)].imported_kwh = 0.165
    report = m.report()
    assert "OVER? (estimate" in report and "BREACH" in report
