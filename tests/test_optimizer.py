"""Scenario tests for the DP optimizer."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.energy_control.optimizer import (
    Action,
    BatteryParams,
    GridParams,
    SlotInput,
    Strategy,
    optimize,
)

T0 = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)


def make_slots(prices, pv=None, load=0.1, export=None):
    """Quarter-hour slots; prices in EUR/kWh; pv/load in kWh per slot."""
    pv = pv or [0.0] * len(prices)
    export = export if export is not None else prices
    return [
        SlotInput(
            start=T0 + timedelta(minutes=15 * i),
            hours=0.25,
            import_price=p,
            export_price=export[i],
            pv_kwh=pv[i],
            load_kwh=load if not isinstance(load, list) else load[i],
        )
        for i, p in enumerate(prices)
    ]


def battery(soc_pct=20.0, **kw):
    cap = kw.pop("capacity_kwh", 10.0)
    return BatteryParams(
        capacity_kwh=cap,
        soc_kwh=cap * soc_pct / 100,
        min_soc_kwh=cap * kw.pop("min_pct", 10) / 100,
        max_soc_kwh=cap * kw.pop("max_pct", 100) / 100,
        max_charge_kw=kw.pop("charge_kw", 3.0),
        max_discharge_kw=kw.pop("discharge_kw", 3.0),
        charge_efficiency=kw.pop("eta_c", 0.95),
        discharge_efficiency=kw.pop("eta_d", 0.95),
        cycle_cost=kw.pop("cycle_cost", 0.02),
    )


def actions(plan):
    return [s.action for s in plan.slots]


def test_cheap_night_expensive_evening_grid_charges():
    prices = [0.20] * 16 + [0.45] * 16  # 4h cheap, 4h expensive
    plan = optimize(make_slots(prices, load=0.3), battery(20), GridParams(), Strategy(), now=T0)
    assert Action.CHARGE_GRID in actions(plan)[:16]
    # Discharges for load during expensive part.
    assert all(s.battery_kw <= 0 for s in plan.slots[16:])
    assert plan.savings > 0


def test_small_spread_no_grid_charge():
    # Spread 2 ct < round-trip losses + wear + margin.
    prices = [0.30] * 16 + [0.32] * 16
    plan = optimize(make_slots(prices, load=0.3), battery(20), GridParams(), Strategy(), now=T0)
    assert Action.CHARGE_GRID not in actions(plan)


def test_grid_charge_disabled():
    prices = [0.10] * 16 + [0.50] * 16
    strat = Strategy(grid_charge=False)
    plan = optimize(make_slots(prices, load=0.3), battery(20), GridParams(), strat, now=T0)
    assert Action.CHARGE_GRID not in actions(plan)
    assert all(s.grid_kw >= -1e-6 or s.battery_kw >= 0 for s in plan.slots)


def test_arbitrage_only_when_allowed_and_profitable():
    prices = [0.10] * 16 + [0.60] * 16
    slots = make_slots(prices, load=0.05)
    no_arb = optimize(slots, battery(20), GridParams(), Strategy(battery_to_grid=False), now=T0)
    assert Action.DISCHARGE_GRID not in actions(no_arb)
    arb = optimize(slots, battery(20), GridParams(), Strategy(battery_to_grid=True), now=T0)
    assert Action.DISCHARGE_GRID in actions(arb)
    assert arb.total_cost < no_arb.total_cost


def test_arbitrage_not_when_unprofitable():
    # 8 ct spread does not cover ~10% losses at 40 ct + 2 ct wear + margins.
    prices = [0.38] * 16 + [0.43] * 16  # 5 ct spread < losses + wear
    plan = optimize(
        make_slots(prices, load=0.05), battery(10, min_pct=10), GridParams(), Strategy(battery_to_grid=True), now=T0
    )
    # Starting empty: buying at 38 ct to use/sell at 43 ct does not pay.
    assert Action.CHARGE_GRID not in actions(plan)
    assert Action.DISCHARGE_GRID not in actions(plan)


def test_negative_export_curtails_and_charges():
    n = 16
    prices = [0.05] * n
    export = [-0.05] * n
    pv = [0.8] * n  # 3.2 kW PV
    plan = optimize(make_slots(prices, pv=pv, load=0.1, export=export), battery(50), GridParams(), Strategy(), now=T0)
    assert any(s.curtail_kw > 0 for s in plan.slots)
    assert all(s.grid_kw >= -1e-6 for s in plan.slots)  # never pays to export


def test_negative_export_no_curtail_when_disabled():
    n = 8
    plan = optimize(
        make_slots([0.05] * n, pv=[0.8] * n, load=0.1, export=[-0.05] * n),
        battery(95),
        GridParams(),
        Strategy(curtail_negative=False),
        now=T0,
    )
    assert all(s.curtail_kw == 0 for s in plan.slots)


def test_soc_bounds_respected():
    prices = [0.05] * 32 + [0.80] * 32
    b = battery(50, min_pct=20, max_pct=90)
    plan = optimize(make_slots(prices, load=0.4), b, GridParams(), Strategy(battery_to_grid=True), now=T0)
    for s in plan.slots:
        assert 20 - 1e-6 <= s.soc_end_pct <= 90 + 1e-6
        assert s.battery_kw <= 3.0 + 1e-6
        assert s.battery_kw >= -3.0 - 1e-6


def test_starts_below_min_soc_does_not_discharge():
    prices = [0.40] * 8
    plan = optimize(make_slots(prices, load=0.3), battery(5, min_pct=10), GridParams(), Strategy(), now=T0)
    assert all(s.battery_kw >= 0 for s in plan.slots)


def test_terminal_value_prevents_dumping():
    # Flat prices: no reason to empty the battery into the grid at the end.
    prices = [0.30] * 16
    plan = optimize(make_slots(prices, load=0.0), battery(80), GridParams(), Strategy(battery_to_grid=True), now=T0)
    assert Action.DISCHARGE_GRID not in actions(plan)


def test_hold_before_peak():
    # Mid prices now, peak later: battery should hold now and discharge in peak.
    prices = [0.30] * 8 + [0.25] * 8 + [0.60] * 8
    plan = optimize(
        make_slots(prices, load=0.4), battery(40, capacity_kwh=5), GridParams(), Strategy(grid_charge=False), now=T0
    )
    assert Action.HOLD in actions(plan)[:16]
    assert sum(s.battery_kw for s in plan.slots[16:]) < 0


@pytest.mark.parametrize("n_slots", [192])
def test_performance(n_slots):
    import math

    prices = [0.25 + 0.15 * math.sin(i / 8) for i in range(n_slots)]
    pv = [max(0.0, 0.6 * math.sin((i % 96 - 28) / 40 * math.pi)) for i in range(n_slots)]
    start = time.perf_counter()
    plan = optimize(
        make_slots(prices, pv=pv, load=0.12), battery(50), GridParams(), Strategy(battery_to_grid=True), now=T0
    )
    assert len(plan.slots) == n_slots
    assert time.perf_counter() - start < 3.0


def test_reported_cost_excludes_arbitrage_hurdle():
    """The arbitrage margin steers the optimizer but is never reported as cost."""
    prices = [0.10] * 16 + [0.60] * 16
    slots = make_slots(prices, load=0.05)
    plan = optimize(slots, battery(20), GridParams(), Strategy(battery_to_grid=True, arbitrage_margin=0.05), now=T0)
    grid_money = sum((s.grid_kw * s.hours) * (s.import_price if s.grid_kw > 0 else s.export_price) for s in plan.slots)
    assert plan.total_cost == pytest.approx(grid_money, abs=1e-6)
    assert plan.total_cost == pytest.approx(sum(s.cost for s in plan.slots))
    assert plan.wear_cost == pytest.approx(sum(s.wear_cost for s in plan.slots))
    discharged = sum(-s.battery_kw * s.hours for s in plan.slots if s.battery_kw < 0)
    assert plan.wear_cost == pytest.approx(discharged * 0.02)


def test_low_load_saved_for_evening_is_hold():
    """Regression (live test 2026-11-15): load below one SoC step, evening dearer.

    The plan keeps the battery at 0 W; that slot must be HOLD, not an "idle"
    SELF_CONSUME that lets the ESS cover the load anyway.
    """
    prices = [0.25] * 16 + [0.40] * 16
    # Scarce stored energy: reserve it for the dearer evening.
    plan = optimize(make_slots(prices, load=0.0625), battery(20), GridParams(), Strategy(), now=T0)
    first = plan.slots[0]
    assert abs(first.battery_kw) < 1e-6
    assert first.grid_kw > 0
    assert first.action == Action.HOLD
    assert first.reason == "save battery for higher prices"


def test_low_load_worth_covering_lets_ess_cover_it():
    """Load below one SoC step while energy is dearest now: the ESS may cover it."""
    prices = [0.40] * 16 + [0.20] * 16
    plan = optimize(make_slots(prices, load=0.0625), battery(43), GridParams(), Strategy(), now=T0)
    covered = [s for s in plan.slots[:16] if s.reason == "battery covers load"]
    assert covered
    for s in covered:
        # Reported as the ESS will execute it: battery supplies the load, no grid import.
        assert s.action == Action.SELF_CONSUME
        assert s.grid_kw == pytest.approx(0.0)
        assert s.battery_kw == pytest.approx(-s.load_kw / 0.95)


def test_at_minimum_soc_with_import_is_hold():
    """At the minimum SoC the ESS must not discharge further (its own minimum may be lower)."""
    prices = [0.30] * 8
    plan = optimize(make_slots(prices, load=0.2), battery(10, min_pct=10), GridParams(), Strategy(), now=T0)
    assert all(s.action == Action.HOLD and s.reason == "battery at minimum SoC" for s in plan.slots)


@pytest.mark.parametrize(
    "prices,pv,load",
    [
        ([0.4] * 16 + [0.2] * 16, None, 0.0625),
        ([0.2] * 16 + [0.4] * 16, [0.01] * 32, 0.0625),
        ([0.1] * 32, [0.3] * 32, 0.02),
        ([-0.1] * 8 + [0.8] * 8, [0.8] * 16, 0.13),
    ],
)
def test_continuous_trajectory_conserves_energy_and_money(prices, pv, load):
    b = battery(43)
    plan = optimize(make_slots(prices, pv=pv, load=load), b, GridParams(), Strategy(), now=T0)
    stored = plan.soc_start_kwh
    for slot in plan.slots:
        assert slot.soc_start_pct == pytest.approx(stored / b.capacity_kwh * 100)
        stored += slot.battery_kw * slot.hours
        assert slot.soc_end_pct == pytest.approx(stored / b.capacity_kwh * 100)
        assert b.min_soc_kwh - 1e-6 <= stored <= b.max_soc_kwh + 1e-6
        assert -b.max_discharge_kw - 1e-6 <= slot.battery_kw <= b.max_charge_kw + 1e-6
        ac_battery = (
            slot.battery_kw / b.charge_efficiency if slot.battery_kw > 0 else (slot.battery_kw * b.discharge_efficiency)
        )
        assert slot.grid_kw == pytest.approx(slot.load_kw + ac_battery - slot.pv_kw + slot.curtail_kw)
        price = slot.import_price if slot.grid_kw > 0 else slot.export_price
        assert slot.cost == pytest.approx(slot.grid_kw * slot.hours * price)
        assert slot.wear_cost == pytest.approx(max(0, -slot.battery_kw * slot.hours) * b.cycle_cost)
    assert plan.soc_end_kwh == pytest.approx(stored)
    assert plan.total_cost == pytest.approx(sum(s.cost for s in plan.slots))
    assert plan.wear_cost == pytest.approx(sum(s.wear_cost for s in plan.slots))


def test_grid_assistance_preserved_when_idle_is_infeasible():
    plan = optimize(make_slots([0.3], load=0.25), battery(50), GridParams(0.1, 0.1), Strategy(), now=T0)
    assert plan.feasible
    assert plan.slots[0].battery_kw < 0
    assert plan.slots[0].grid_kw <= 0.1 + 1e-6


def test_unavoidable_overload_is_explicit():
    plan = optimize(make_slots([0.3], load=0.25), battery(10), GridParams(0.1, 0.1), Strategy(), now=T0)
    assert not plan.feasible
    assert plan.slots[0].grid_violation_kw == pytest.approx(0.9)
    assert "unavoidable" in plan.slots[0].reason
    assert plan.as_dict()["feasible"] is False


def test_grid_export_limit_curtails_pv_and_preserves_strategy():
    plan = optimize(
        make_slots([0.3], pv=[2], load=0.0),
        battery(100),
        GridParams(0.1, 0.1),
        Strategy(grid_charge=False, battery_to_grid=False, curtail_negative=False),
        now=T0,
    )
    assert plan.feasible
    assert plan.slots[0].grid_kw >= -0.1 - 1e-6
    assert plan.slots[0].curtail_kw > 0
