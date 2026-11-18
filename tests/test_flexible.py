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
