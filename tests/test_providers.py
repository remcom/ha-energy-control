"""Provider parsing tests (fixtures captured from a live install)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_control.providers.load import LoadProfile, build_load_profile
from custom_components.energy_control.providers.prices import (
    ExportMode,
    PriceModel,
    parse_price_attributes,
    price_at,
    unit_scale,
)
from custom_components.energy_control.providers.slots import build_slots
from custom_components.energy_control.providers.solar import (
    energy_between,
    parse_pv_attributes,
)

FIX = Path(__file__).parent / "fixtures"
AMS = ZoneInfo("Europe/Amsterdam")


def load(name):
    return json.loads((FIX / name).read_text())


def test_unit_scale():
    assert unit_scale("ct/kWh") == 0.01
    assert unit_scale("EUR/kWh") == 1.0
    assert unit_scale("€/MWh") == pytest.approx(0.001)


def test_tibber_prices_quarters():
    points = parse_price_attributes(load("tibber_prices.json"))
    assert len(points) == 10
    assert points[0].minutes == 15
    assert points[0].price == pytest.approx(0.4205)
    assert points[0].spot == pytest.approx(0.2354)
    assert points[1].start - points[0].start == timedelta(minutes=15)


def test_generic_hourly_list():
    attrs = {
        "unit_of_measurement": "EUR/kWh",
        "prices": [
            {"time": "2026-11-15T00:00:00+01:00", "price": 0.25},
            {"time": "2026-11-15T01:00:00+01:00", "price": 0.20},
        ],
    }
    points = parse_price_attributes(attrs)
    assert [p.minutes for p in points] == [60, 60]
    assert price_at(points, datetime(2026, 11, 15, 0, 30, tzinfo=AMS)).price == 0.25


def test_nordpool_raw():
    attrs = {
        "unit_of_measurement": "EUR/kWh",
        "raw_today": [
            {"start": "2026-11-15T00:00:00+01:00", "end": "2026-11-15T00:15:00+01:00", "value": 0.1},
            {"start": "2026-11-15T00:15:00+01:00", "end": "2026-11-15T00:30:00+01:00", "value": 0.2},
        ],
        "raw_tomorrow": [],
    }
    points = parse_price_attributes(attrs)
    assert [p.minutes for p in points] == [15, 15]


def test_export_models():
    point = parse_price_attributes(load("tibber_prices.json"))[0]
    netting = PriceModel(export_mode=ExportMode.NETTING, netting_end=date(2027, 1, 1))
    assert netting.export_price(point, date(2026, 11, 14)) == pytest.approx(0.4205)
    # After saldering ends: spot - fee.
    netting_fee = PriceModel(export_mode=ExportMode.NETTING, netting_end=date(2027, 1, 1), export_fee=0.02)
    assert netting_fee.export_price(point, date(2027, 1, 2)) == pytest.approx(0.2154)
    fixed = PriceModel(export_mode=ExportMode.FIXED, export_fixed=0.05)
    assert fixed.export_price(point, date(2026, 11, 14)) == 0.05
    spot_only = PriceModel(includes_taxes=False, markup=0.02, energy_tax=0.10, vat=0.21)
    assert spot_only.import_price(point) == pytest.approx((0.4205 + 0.12) * 1.21)


def test_solcast_parse_and_spread():
    intervals = parse_pv_attributes(load("solcast_tomorrow.json"))
    assert len(intervals) == 3
    assert intervals[0].minutes == 30
    assert intervals[0].kwh == pytest.approx(0.2403)
    q = datetime(2026, 11, 15, 11, 0, tzinfo=AMS)
    assert energy_between(intervals, q, q + timedelta(minutes=15)) == pytest.approx(0.12015)
    p10 = parse_pv_attributes(load("solcast_tomorrow.json"), "pv_estimate10")
    assert p10[0].kwh == pytest.approx(0.0631)


def test_wh_period():
    attrs = {"wh_period": {"2026-11-15T12:00:00+01:00": 800, "2026-11-15T13:00:00+01:00": 400}}
    intervals = parse_pv_attributes(attrs)
    assert intervals[0].kwh == 0.8 and intervals[0].minutes == 60


def test_load_profile():
    samples = [
        (datetime(2026, 11, 9, 17, 0, tzinfo=AMS), 1.0),  # Monday
        (datetime(2026, 11, 10, 17, 0, tzinfo=AMS), 2.0),  # Tuesday
        (datetime(2026, 11, 14, 17, 0, tzinfo=AMS), 5.0),  # Saturday
    ]
    profile = build_load_profile(samples, AMS)
    assert profile.at(datetime(2026, 11, 11, 17, 30, tzinfo=AMS)) == 1.5
    assert profile.at(datetime(2026, 11, 15, 17, 30, tzinfo=AMS)) == 5.0
    assert profile.at(datetime(2026, 11, 11, 3, 0, tzinfo=AMS)) == pytest.approx(8 / 3)


def test_build_slots_partial_first_slot():
    prices = parse_price_attributes(load("tibber_prices.json"))
    now = datetime(2026, 11, 14, 16, 5, tzinfo=AMS)
    slots = build_slots(now, prices, [], LoadProfile(AMS, fallback_kw=0.4), PriceModel(), AMS)
    assert slots[0].hours == pytest.approx(10 / 60)
    assert slots[0].load_kwh == pytest.approx(0.4 * 10 / 60)
    assert slots[1].start == datetime(2026, 11, 14, 16, 15, tzinfo=AMS)
    assert len(slots) == 10
    assert slots[-1].start.astimezone(UTC) == datetime(2026, 11, 14, 17, 15, tzinfo=UTC)
