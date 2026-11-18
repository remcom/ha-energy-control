"""Provider parsing tests (fixtures captured from a live install)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.energy_control.optimizer import SlotInput
from custom_components.energy_control.providers.load import LoadProfile, build_load_profile
from custom_components.energy_control.providers.prices import (
    ExportMode,
    PriceModel,
    parse_price_attributes,
    price_at,
    unit_scale,
)
from custom_components.energy_control.providers.slots import Corrections, apply_corrections, build_slots
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


def test_load_profile_trims_one_off_peaks():
    # Ten weekday 18:00 samples, one with an EV session.
    samples = [(datetime(2026, 11, 2 + d + (2 if d >= 5 else 0), 18, 0, tzinfo=AMS), 0.5) for d in range(10)]
    samples[3] = (samples[3][0], 11.0)
    profile = build_load_profile(samples, AMS)
    assert profile.at(datetime(2026, 11, 18, 18, 0, tzinfo=AMS)) == pytest.approx(0.5)


def test_corrections_live_running_slot_and_decay():
    now = datetime(2026, 11, 14, 12, 5, tzinfo=AMS)
    slots = [SlotInput(now + timedelta(minutes=15 * i - (5 if i else 0)), 0.25, 0.3, 0.3, 0.5, 0.1) for i in range(20)]
    slots[0] = SlotInput(now, 10 / 60, 0.3, 0.3, 0.5 * 2 / 3, 0.1 * 2 / 3)
    out = apply_corrections(slots, now, Corrections(pv_factor=0.5, live_pv_kw=0.4, live_load_kw=1.2))
    assert out[0].pv_kwh == pytest.approx(0.4 * 10 / 60)  # live replaces the forecast
    assert out[0].load_kwh == pytest.approx(1.2 * 10 / 60)
    assert 0.1 < out[1].load_kwh / 0.25 < 1.2  # next slot: half live, half forecast
    assert out[4].pv_kwh < 0.5  # factor still applies an hour ahead
    assert out[-1].pv_kwh == pytest.approx(0.5)  # faded out after 3 h
    assert [s.pv_forecast_kwh for s in out] == [s.pv_forecast_kwh for s in slots]


def test_corrections_no_live_pv_when_forecast_dark():
    now = datetime(2026, 11, 14, 17, 0, tzinfo=AMS)
    slot = SlotInput(now, 0.25, 0.3, 0.3, 0.0, 0.1)
    assert apply_corrections([slot], now, Corrections(live_pv_kw=0.2))[0].pv_kwh == 0.0


def test_build_slots_partial_first_slot():
    prices = parse_price_attributes(load("tibber_prices.json"))
    now = datetime(2026, 11, 14, 16, 5, tzinfo=AMS)
    slots = build_slots(now, prices, [], LoadProfile(AMS, fallback_kw=0.4), PriceModel(), AMS)
    assert slots[0].hours == pytest.approx(10 / 60)
    assert slots[0].load_kwh == pytest.approx(0.4 * 10 / 60)
    assert slots[1].start == datetime(2026, 11, 14, 16, 15, tzinfo=AMS)
    assert len(slots) == 10
    assert slots[-1].start.astimezone(UTC) == datetime(2026, 11, 14, 17, 15, tzinfo=UTC)


def test_mixed_resolution_prices_do_not_overlap():
    """An hourly list next to quarter prices is shortened so intervals never overlap."""
    attrs = {
        "unit_of_measurement": "EUR/kWh",
        "prices": [
            {"start": "2026-11-15T10:15:00+00:00", "end": "2026-11-15T10:30:00+00:00", "price": 0.30},
            {"start": "2026-11-15T10:30:00+00:00", "end": "2026-11-15T10:45:00+00:00", "price": 0.40},
        ],
        "raw_today": [{"start": "2026-11-15T10:00:00+00:00", "end": "2026-11-15T11:00:00+00:00", "value": 0.20}],
    }
    points = parse_price_attributes(attrs)
    for prev, nxt in itertools.pairwise(points):
        assert prev.end <= nxt.start
    assert points[0].minutes == 15
    assert price_at(points, datetime(2026, 11, 15, 10, 20, tzinfo=UTC)).price == 0.30


def test_pv_parsers_skip_non_numeric_values():
    """A broken forecast value is skipped instead of failing the whole plan."""
    solcast = {
        "detailedForecast": [
            {"period_start": "2026-11-15T10:00:00+00:00", "pv_estimate": "unavailable"},
            {"period_start": "2026-11-15T10:30:00+00:00", "pv_estimate": 1.0},
        ]
    }
    assert [i.kwh for i in parse_pv_attributes(solcast)] == [0.5]
    wh = {"wh_period": {"2026-11-15T10:00:00+00:00": "n/a", "2026-11-15T11:00:00+00:00": 500}}
    assert [i.kwh for i in parse_pv_attributes(wh)] == [0.5]


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", float("nan"), float("inf")])
def test_nonfinite_prices_and_pv_are_rejected(value):
    assert parse_price_attributes({"outlook_quarters": {"start": "2026-11-15T00:00:00Z", "prices": [value]}}) == []
    assert parse_pv_attributes({"forecast": [{"start": "2026-11-15T00:00:00Z", "value": value}]}) == []


@pytest.mark.parametrize("minutes", [-1, 0, "nan", "bad", 0.5, 2000])
def test_invalid_quarter_duration_is_rejected(minutes):
    assert (
        parse_price_attributes(
            {
                "outlook_quarters": {
                    "start": "2026-11-15T00:00:00Z",
                    "prices": [0.3],
                    "minutes": minutes,
                }
            }
        )
        == []
    )


@pytest.mark.parametrize("unit", ["USD/kWh", "SEK/MWh", "EUR", "W", "EUR/Wh"])
def test_unknown_price_currency_or_unit_requires_explicit_conversion(unit):
    with pytest.raises(ValueError, match="Unsupported price"):
        parse_price_attributes({"prices": []}, unit)


def test_unknown_all_in_export_never_assumes_import_is_spot():
    from custom_components.energy_control.providers.prices import PricePoint

    p = PricePoint(datetime(2027, 1, 1, tzinfo=AMS), 60, 0.3)
    model = PriceModel(netting_end=date(2027, 1, 1))
    assert model.export_price(p, date(2026, 12, 31)) == 0.3
    assert model.export_price(p, date(2027, 1, 1)) is None
    assert PriceModel(export_mode=ExportMode.SPOT).export_price(p, date(2026, 11, 15)) is None
    assert PriceModel(export_mode=ExportMode.FIXED, export_fixed=0.05).export_price(p, date(2027, 1, 1)) == 0.05
    assert PriceModel(export_mode=ExportMode.SPOT, includes_taxes=False).export_price(p, date(2027, 1, 1)) == 0.3


def test_netting_transition_at_local_midnight_with_negative_raw_spot():
    from custom_components.energy_control.providers.prices import PricePoint

    start = datetime(2026, 12, 31, 23, 45, tzinfo=AMS)
    prices = [PricePoint(start, 15, 0.3, -0.05), PricePoint(start + timedelta(minutes=15), 15, 0.3, -0.05)]
    slots = build_slots(start, prices, [], LoadProfile(AMS), PriceModel(netting_end=date(2027, 1, 1)), AMS)
    assert slots[0].export_price == 0.3
    assert slots[1].export_price == -0.05


def test_missing_spot_is_explicit_and_disables_price_dependent_battery_export():
    from custom_components.energy_control.optimizer import GridParams, Strategy, optimize
    from custom_components.energy_control.providers.prices import PricePoint

    from .test_optimizer import battery

    start = datetime(2027, 1, 1, tzinfo=AMS)
    prices = [PricePoint(start, 60, 0.9)]
    slots = build_slots(
        start, prices, [], LoadProfile(AMS, fallback_kw=0), PriceModel(netting_end=date(2027, 1, 1)), AMS
    )
    assert all(not s.export_price_known for s in slots)
    plan = optimize(
        slots,
        battery(80),
        GridParams(),
        Strategy(battery_to_grid=True),
        now=start,
    )
    assert not plan.export_prices_complete
    assert all(s.battery_kw >= 0 for s in plan.slots)
