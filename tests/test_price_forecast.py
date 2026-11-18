"""Price estimates beyond the published day-ahead prices."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_control.optimizer import BatteryParams, GridParams, SlotInput, Strategy, optimize
from custom_components.energy_control.providers.price_forecast import (
    Observation,
    PriceEstimator,
    Weather,
    calibrate_external,
    hour_key,
    spot_relation,
    with_spot,
)
from custom_components.energy_control.providers.prices import PricePoint

AMS = ZoneInfo("Europe/Amsterdam")
NOW = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)


def synthetic(days=21, solar_effect=-0.10, wind_effect=-0.05):
    """Prices with an evening peak, a solar dip and a windy-day discount."""
    rng = random.Random(1)
    weather, rows, truth = {}, [], {}
    day_wind = {}
    t = NOW - timedelta(days=days)
    while t < NOW + timedelta(days=2):
        local = t.astimezone(AMS)
        day_wind.setdefault(local.date(), rng.uniform(2, 14))
        sun = max(0.0, math.sin((local.hour - 7) / 10 * math.pi)) * rng.uniform(0.2, 1.0) * 3
        weather[hour_key(t)] = Weather(solar_kw=sun, wind_ms=day_wind[local.date()])
        t += timedelta(hours=1)
    t = NOW - timedelta(days=days)
    while t < NOW + timedelta(days=2):
        local = t.astimezone(AMS)
        w = weather[hour_key(t)]
        peak = 0.10 if 17 <= local.hour < 21 else 0.0
        value = 0.25 + peak + solar_effect * (w.solar_kw / 3 - 0.3) + wind_effect * (w.wind_ms / 10 - 0.8)
        truth[t] = value
        if t < NOW:
            rows.append(Observation(t, value + rng.gauss(0, 0.01)))
        t += timedelta(minutes=15)
    return rows, weather, truth


def test_estimates_track_peak_and_weather():
    rows, weather, truth = synthetic()
    est = PriceEstimator(rows, weather, AMS, NOW)
    assert est.usable
    points = est.estimate(NOW + timedelta(hours=13), NOW + timedelta(hours=37), [])
    assert len(points) == 96 and all(p.estimated for p in points)
    errors = [abs(p.price - truth[p.start]) for p in points]
    assert sum(errors) / len(errors) < 0.015  # mean error below 1.5 ct
    evening = [p.price for p in points if 17 <= p.start.astimezone(AMS).hour < 21]
    night = [p.price for p in points if 1 <= p.start.astimezone(AMS).hour < 5]
    assert min(evening) > max(night) + 0.05  # tomorrow's evening peak is visible


def test_not_usable_with_little_history():
    rows, weather, _ = synthetic(days=2)
    assert not PriceEstimator(rows, weather, AMS, NOW).usable


def test_estimates_stay_within_observed_range():
    rows = [Observation(NOW - timedelta(minutes=15 * i), 0.2 + 0.001 * (i % 50)) for i in range(1, 600)]
    weather = {hour_key(NOW + timedelta(hours=h)): Weather(wind_ms=200.0) for h in range(48)}
    points = PriceEstimator(rows, weather, AMS, NOW).estimate(NOW, NOW + timedelta(hours=24), [])
    assert all(0.19 <= p.price <= 0.26 for p in points)  # absurd wind does not explode


def _points(values, spot=None, start=NOW, minutes=15):
    return [
        PricePoint(start + timedelta(minutes=minutes * i), minutes, v, None if spot is None else spot[i])
        for i, v in enumerate(values)
    ]


def test_external_forecast_calibrated_on_overlap():
    spot = [0.05 + 0.01 * i for i in range(12)]
    published = _points([0.15 + 1.21 * s for s in spot])
    external = _points(spot)  # spot-only forecast covering the same quarters
    convert = calibrate_external(external, published)
    assert convert(0.10) == pytest.approx(0.15 + 0.121)


def test_external_forecast_without_overlap_uses_spot_relation():
    spot = [0.05 + 0.01 * i for i in range(12)]
    published = _points([0.15 + 1.21 * s for s in spot], spot)
    external = _points([0.10], start=NOW + timedelta(days=1))
    assert calibrate_external(external, published)(0.10) == pytest.approx(0.271)
    relation = spot_relation(published)
    derived = with_spot(_points([0.271]), relation)
    assert derived[0].spot == pytest.approx(0.10) and derived[0].estimated


def test_plan_money_excludes_estimated_quarters():
    slots = [
        SlotInput(NOW + timedelta(minutes=15 * i), 0.25, 0.30, 0.30, 0.0, 0.4, price_estimated=i >= 4) for i in range(8)
    ]
    battery = BatteryParams(10, 5, 1, 10, 3, 3)
    plan = optimize(slots, battery, GridParams(), Strategy(), now=NOW)
    assert plan.estimated_from == NOW + timedelta(hours=1)
    assert plan.baseline_cost == pytest.approx(4 * 0.4 * 0.30)  # load_kwh is energy per quarter
    assert plan.as_dict()["estimated_from"] is not None
