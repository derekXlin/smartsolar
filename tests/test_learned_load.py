"""Evening load learned from the controller's own samples."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from zerohero_dynamic_control.config import AppConfig
from zerohero_dynamic_control.data_providers.learned_load import LearnedLoadForecastProvider
from zerohero_dynamic_control.data_providers.static_forecast import StaticForecastProvider

from .conftest import TZ

TODAY = datetime(2026, 10, 1, 17, 50, tzinfo=TZ)


def _cfg() -> AppConfig:
    cfg = AppConfig()
    # The guessed profile this site shipped with.
    cfg.forecast.load_profile_kw = {"17": 2.2, "18": 2.7, "19": 2.9, "20": 2.4}
    return cfg


def _write_evenings(path, loads_by_day: dict[int, float], *, poll_repeats: int = 5) -> None:
    """Samples like the live loop writes: polled every minute, a new cloud
    snapshot every five, 18:00-21:00."""
    with path.open("w", encoding="utf-8") as fh:
        for days_ago, kw in loads_by_day.items():
            start = (TODAY - timedelta(days=days_ago)).replace(hour=18, minute=0)
            for k in range(36):
                measured = start + timedelta(minutes=5 * k)
                for j in range(poll_repeats):
                    polled = measured + timedelta(minutes=j, seconds=40)
                    fh.write(json.dumps({"timestamp": polled.isoformat(), "measured_at": measured.isoformat(),
                                         "load_kw": kw, "grid_kw": 0.0}) + "\n")


def _provider(tmp_path, **kw) -> LearnedLoadForecastProvider:
    cfg = _cfg()
    return LearnedLoadForecastProvider(StaticForecastProvider(cfg), tmp_path / "samples.jsonl", tz=TZ, **kw)


@pytest.mark.asyncio
async def test_recent_evenings_replace_the_guessed_profile(tmp_path):
    """28-30 Sep drew 1.9-2.4 kW against a guessed 2.7-2.9 kW."""
    _write_evenings(tmp_path / "samples.jsonl", {1: 1.9, 2: 2.0, 3: 2.4})
    p = _provider(tmp_path)
    pts = await p.load(TODAY, TODAY.replace(hour=21))
    by_time = {pt.timestamp.strftime("%H:%M"): pt.load_kw for pt in pts}
    assert by_time["18:05"] == pytest.approx(2.1)
    assert by_time["20:50"] == pytest.approx(2.1)
    assert by_time["17:50"] == pytest.approx(2.2), "no history before 18:00: keep the profile"
    assert p.load_note and "3 recent evenings" in p.load_note and "18:00 2.1 kW" in p.load_note


@pytest.mark.asyncio
async def test_too_little_history_keeps_the_profile(tmp_path):
    _write_evenings(tmp_path / "samples.jsonl", {1: 1.5, 2: 1.5})
    p = _provider(tmp_path, min_days=3)
    pts = await p.load(TODAY, TODAY.replace(hour=21))
    assert {pt.timestamp.strftime("%H:%M"): pt.load_kw for pt in pts}["18:35"] == pytest.approx(2.7)
    assert p.load_note is None


@pytest.mark.asyncio
async def test_old_evenings_and_today_are_left_out(tmp_path):
    """Only the last ``days`` complete evenings: a replan at 19:00 must not learn
    from the first hour of the evening it is planning."""
    _write_evenings(tmp_path / "samples.jsonl", {0: 9.0, 1: 2.0, 2: 2.0, 3: 2.0, 30: 9.0})
    p = _provider(tmp_path, days=7)
    pts = await p.load(TODAY, TODAY.replace(hour=21))
    assert {pt.timestamp.strftime("%H:%M"): pt.load_kw for pt in pts}["19:05"] == pytest.approx(2.0)


def test_a_snapshot_polled_again_counts_once(tmp_path):
    """One evening, one slot: a 4 kW snapshot polled five times and a 1 kW one
    polled once is a 2.5 kW slot, not 3.5."""
    start = (TODAY - timedelta(days=1)).replace(hour=18, minute=0)
    lines = [(start + timedelta(minutes=j), start, 4.0) for j in range(5)]
    lines.append((start + timedelta(minutes=5), start + timedelta(minutes=5), 1.0))
    with (tmp_path / "samples.jsonl").open("w") as fh:
        for polled, measured, kw in lines:
            fh.write(json.dumps({"timestamp": polled.isoformat(), "measured_at": measured.isoformat(),
                                 "load_kw": kw, "grid_kw": 0.0}) + "\n")
    p = _provider(tmp_path, min_days=1)
    kw, evenings = p.profile(TODAY.date())[(18 * 60) // 15]
    assert kw == pytest.approx(2.5) and evenings == 1


def test_learning_is_on_by_default_and_can_be_turned_off():
    from zerohero_dynamic_control.data_providers import build_forecast_provider

    cfg = AppConfig()
    assert isinstance(build_forecast_provider(cfg), LearnedLoadForecastProvider)
    cfg.forecast.learn_load_days = 0
    assert not isinstance(build_forecast_provider(cfg), LearnedLoadForecastProvider)
