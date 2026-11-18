"""Flexible loads: cheapest window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_control.flexible.window import cheapest_window, marginal_cost
from custom_components.energy_control.optimizer import Action, SlotPlan

T0 = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)


def slot(i, price, grid_kw=0.5, export=None, estimated=False):
    return SlotPlan(
        T0 + timedelta(minutes=15 * i),
        0.25,
        Action.SELF_CONSUME,
        0,
        grid_kw,
        0,
        0.4,
        0,
        50,
        50,
        price,
        price if export is None else export,
        0,
        price_estimated=estimated,
    )


def test_exported_pv_counts_at_export_price():
    s = slot(0, 0.30, grid_kw=-2.0, export=0.05)  # 0.5 kWh exported this quarter
    assert marginal_cost(s, 0.25) == pytest.approx(0.25 * 0.05)
    assert marginal_cost(s, 1.0) == pytest.approx(0.5 * 0.05 + 0.5 * 0.30)


def test_contiguous_window_picks_cheapest_block():
    prices = [0.30] * 8 + [0.10, 0.12, 0.11, 0.09] + [0.30] * 8
    slots = [slot(i, p) for i, p in enumerate(prices)]
    w = cheapest_window(slots, timedelta(hours=1), 2.0, earliest=T0)
    assert w.start == T0 + timedelta(hours=2)
    assert w.energy_kwh == pytest.approx(2.0)
    assert w.average_price == pytest.approx(0.105)


def test_window_respects_deadline_and_earliest():
    prices = [0.20, 0.10, 0.30, 0.05]
    slots = [slot(i, p) for i, p in enumerate(prices)]
    w = cheapest_window(slots, timedelta(minutes=15), 1.0, earliest=T0, latest_end=T0 + timedelta(minutes=45))
    assert w.start == T0 + timedelta(minutes=15)
    w = cheapest_window(slots, timedelta(minutes=15), 1.0, earliest=T0 + timedelta(minutes=20))
    assert w.start == T0 + timedelta(minutes=45)
    assert cheapest_window(slots, timedelta(hours=2), 1.0, earliest=T0) is None


def test_non_contiguous_spreads_over_cheapest_quarters():
    prices = [0.10, 0.30, 0.12, 0.30, 0.11, 0.30]
    slots = [slot(i, p, estimated=i == 4) for i, p in enumerate(prices)]
    w = cheapest_window(slots, timedelta(minutes=45), 4.0, earliest=T0, contiguous=False)
    assert [r[0] for r in w.slots] == [T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=60)]
    assert w.average_price == pytest.approx(0.11)
    assert w.price_estimated


# --- heat pump ---------------------------------------------------------------
from custom_components.energy_control.flexible.ev import deadline, schedule_ev  # noqa: E402
from custom_components.energy_control.flexible.heatpump import (  # noqa: E402
    HeatPumpParams,
    HpMode,
    HpSlot,
    block_allowance,
    load_with_heat_pump,
    max_block,
    schedule_heat_pump,
)
from custom_components.energy_control.flexible.manager import sg_ready_options  # noqa: E402

PARAMS = HeatPumpParams(comfort=False)


def hp_slots(prices, temp=10.0, power=1.0):
    return [HpSlot(slot(i, p), power, temp) for i, p in enumerate(prices)]


def test_block_length_shrinks_with_outdoor_temperature():
    assert max_block(12.0, PARAMS) == timedelta(minutes=120)
    assert max_block(7.0, PARAMS) == timedelta(minutes=60)
    assert max_block(2.0, PARAMS) == timedelta(0)
    assert max_block(3.0, PARAMS) == timedelta(0)  # 12 min: shorter than a useful block
    assert block_allowance(None, PARAMS) == 0.0  # unknown temperature: never block


def test_blocks_expensive_peak_when_catch_up_is_cheaper():
    prices = [0.25] * 8 + [0.45] * 8 + [0.20] * 16
    modes = schedule_heat_pump(hp_slots(prices, temp=12.0), PARAMS, now=T0)
    blocked = [i for i, m in enumerate(modes) if m == HpMode.BLOCK]
    assert blocked and set(blocked) <= set(range(8, 16))
    assert len(blocked) <= 8  # at most 2 h at 12 °C
    after = max(blocked) + 1
    assert all(m == HpMode.NORMAL for m in modes[after : after + len(blocked)])  # recovery


def test_no_block_when_cold_or_flat():
    prices = [0.25] * 8 + [0.45] * 8 + [0.20] * 16
    assert HpMode.BLOCK not in schedule_heat_pump(hp_slots(prices, temp=1.0), PARAMS, now=T0)
    assert HpMode.BLOCK not in schedule_heat_pump(hp_slots([0.25] * 32, temp=12.0), PARAMS, now=T0)


def test_daily_budget_and_running_block_limit():
    prices = [0.45, 0.45, 0.45, 0.45, 0.20, 0.20, 0.20, 0.20] * 6
    modes = schedule_heat_pump(hp_slots(prices, temp=12.0), PARAMS, now=T0, blocked_today_h=5.5)
    assert modes.count(HpMode.BLOCK) <= 2  # 30 min of budget left
    # A block running for 1h50 at 12 °C may only continue for 10 more minutes (one quarter).
    modes = schedule_heat_pump(hp_slots(prices, temp=12.0), PARAMS, now=T0, blocked_since=T0 - timedelta(minutes=110))
    assert modes[0] == HpMode.BLOCK and modes[1] != HpMode.BLOCK


def test_rebound_shifts_blocked_energy():
    slots = hp_slots([0.45] * 4 + [0.20] * 4, power=1.0)
    modes = [HpMode.BLOCK] * 4 + [HpMode.NORMAL] * 4
    energy = load_with_heat_pump(slots, modes, PARAMS)
    assert energy[:4] == [0.0] * 4
    assert sum(energy) == pytest.approx(1.0 + 1.0 * (1 + PARAMS.rebound_penalty))  # 1 kWh shifted, +5 %


def test_comfort_when_pv_is_exported():
    slots = [HpSlot(slot(i, 0.30, grid_kw=-2.0, export=0.05), 0.5, 10.0) for i in range(8)]
    modes = schedule_heat_pump(slots, HeatPumpParams(), now=T0)
    assert modes.count(HpMode.COMFORT) == 8


def test_sg_ready_option_mapping():
    mapping = sg_ready_options(["Normal mode", "EVU mode", "Comfort mode", "Boost mode"])
    assert mapping == {
        HpMode.NORMAL: "Normal mode",
        HpMode.BLOCK: "EVU mode",
        HpMode.COMFORT: "Comfort mode",
        HpMode.BOOST: "Boost mode",
    }


# --- EV -----------------------------------------------------------------------
def test_ev_charges_cheapest_quarters_before_deadline():
    prices = [0.30, 0.10, 0.30, 0.12, 0.05, 0.30, 0.01, 0.01]
    slots = [slot(i, p) for i, p in enumerate(prices)]
    ready = T0 + timedelta(minutes=90)  # last two (cheapest) quarters are too late
    chosen = schedule_ev(slots, 2.0, 4.0, now=T0, ready_by=ready)
    assert chosen == {T0 + timedelta(minutes=15), T0 + timedelta(minutes=60)}


def test_ev_charges_immediately_when_short_of_time():
    slots = [slot(i, 0.2) for i in range(8)]
    chosen = schedule_ev(slots, 50.0, 4.0, now=T0, ready_by=T0 + timedelta(hours=1))
    assert chosen == {T0 + timedelta(minutes=15 * i) for i in range(4)}


def test_deadline_is_next_occurrence():
    from datetime import time
    from zoneinfo import ZoneInfo

    ams = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 11, 18, 8, 0, tzinfo=ams)
    assert deadline(now, time(7, 0), ams) == datetime(2026, 11, 19, 7, 0, tzinfo=ams)
    assert deadline(now, time(9, 0), ams) == datetime(2026, 11, 18, 9, 0, tzinfo=ams)


# --- heat pump model ------------------------------------------------------------
def test_heat_pump_model_from_daily_averages():
    """Real daily means of a heat pump (W) against outdoor temperature (C)."""
    from zoneinfo import ZoneInfo

    from custom_components.energy_control.flexible.hp_model import fit_heat_pump
    from custom_components.energy_control.providers.price_forecast import hour_key

    ams = ZoneInfo("Europe/Amsterdam")
    watts = [266.8, 359.6, 407.9, 467.1, 251.0, 304.0, 492.2, 401.1, 393.6, 405.7, 533.2, 419.7, 250.7, 351.0, 149.7]
    temps = [6.83, 5.03, 3.81, 2.81, 6.61, 5.89, 2.55, 3.68, 4.22, 4.06, 2.29, 4.24, 6.80, 5.69, 8.89]
    shape = [1.4] * 8 + [0.6] * 16  # runs harder at night
    norm = sum(shape) / 24
    power, temp_map = [], {}
    for d, (w, t) in enumerate(zip(watts, temps, strict=True)):
        for h in range(24):
            when = datetime(2026, 11, 4 + d, h, 0, tzinfo=ams)
            power.append((when, w / 1000 * shape[h] / norm))
            temp_map[hour_key(when)] = t
    model = fit_heat_pump(power, temp_map, ams)
    assert model.b * 1000 == pytest.approx(54.6, abs=1)
    assert model.balance_temp == pytest.approx(11.5, abs=0.3)
    assert model.r2 > 0.95
    assert model.daily_kw(7.5) == pytest.approx(0.221, abs=0.01)
    assert model.daily_kw(14.0) == 0.0  # no heating on mild days
    assert model.power(2.0, 3) > model.power(2.0, 15) * 2  # night hours weigh more
    assert sum(model.shape.values()) / len(model.shape) == pytest.approx(1.0)
