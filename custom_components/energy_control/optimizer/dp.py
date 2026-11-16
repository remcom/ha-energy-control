"""Dynamic-programming battery schedule optimizer.

State: stored battery energy, discretized in `soc_step_kwh` steps anchored at the
current SoC. Stage: time slot. Decision: change of stored energy in the slot.

For every slot the cost of each possible SoC change is independent of the SoC
itself, so it is computed once per slot and the DP inner loop is a plain
min-plus over (state, delta). With 192 slots x ~90 states x ~20 deltas this
takes well under a second in pure Python.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .model import (
    Action,
    BatteryParams,
    GridParams,
    Plan,
    SlotInput,
    SlotPlan,
    Strategy,
)

EPS = 1e-6
MIN_FLOW_KWH = 1e-3  # grid energy per slot below this counts as none
INF = math.inf


@dataclass(slots=True)
class _Flow:
    """Energy flows (kWh) of one slot for one SoC delta."""

    cost: float  # optimizer objective: money + wear + arbitrage hurdle
    money: float  # EUR actually paid for grid energy (import - export)
    wear: float  # EUR battery wear
    grid_import: float
    grid_export: float
    pv_used: float
    curtailed: float
    grid_charged: float
    battery_exported: float


def slot_flow(
    slot: SlotInput,
    delta_kwh: float,
    battery: BatteryParams,
    grid: GridParams,
    strategy: Strategy,
) -> _Flow | None:
    """Cheapest feasible energy flow for a stored-energy change, or None."""
    if delta_kwh > 0:
        battery_ac = delta_kwh / battery.charge_efficiency
    else:
        battery_ac = delta_kwh * battery.discharge_efficiency  # negative = delivering

    pv = slot.pv_kwh
    load = slot.load_kwh
    max_import = grid.max_import_kw * slot.hours
    max_export = grid.max_export_kw * slot.hours

    candidates = [pv]
    if strategy.curtail_negative and (slot.export_price < 0 or slot.import_price < 0):
        candidates.append(max(0.0, min(pv, load + battery_ac)))  # zero export
        candidates.append(0.0)  # import everything (negative import price)

    best: _Flow | None = None
    for pv_used in candidates:
        net = load + battery_ac - pv_used
        grid_import = max(net, 0.0)
        grid_export = max(-net, 0.0)
        if grid_import > max_import + EPS:
            continue
        curtailed = pv - pv_used
        if grid_export > max_export + EPS:
            # The inverter/GX limits feed-in by curtailing PV.
            excess = grid_export - max_export
            if pv_used - excess < -EPS:
                continue  # battery alone would exceed the export limit
            curtailed += excess
            pv_used -= excess
            grid_export = max_export

        grid_charged = min(battery_ac, grid_import) if delta_kwh > 0 else 0.0
        battery_exported = min(-battery_ac, grid_export) if delta_kwh < 0 else 0.0
        if not strategy.grid_charge and grid_charged > EPS:
            continue
        if not strategy.battery_to_grid and battery_exported > EPS:
            continue

        money = grid_import * slot.import_price - grid_export * slot.export_price
        wear = max(-delta_kwh, 0.0) * battery.cycle_cost
        cost = money + wear + (grid_charged + battery_exported) * strategy.arbitrage_margin
        if best is None or cost < best.cost - EPS:
            best = _Flow(
                cost,
                money,
                wear,
                grid_import,
                grid_export,
                pv_used,
                curtailed,
                grid_charged,
                battery_exported,
            )
    return best


def baseline_cost(slots: list[SlotInput]) -> float:
    """Cost without battery and without curtailment."""
    total = 0.0
    for slot in slots:
        net = slot.load_kwh - slot.pv_kwh
        if net >= 0:
            total += net * slot.import_price
        else:
            total += net * slot.export_price
    return total


def terminal_value(slots: list[SlotInput], battery: BatteryParams) -> float:
    """EUR/kWh value of energy left in the battery at the end of the horizon.

    Conservative: the 25th percentile import price, after discharge losses and
    wear. Prevents the optimizer from emptying the battery at the horizon end
    while not encouraging it to hoard.
    """
    if not slots:
        return 0.0
    prices = sorted(slot.import_price for slot in slots)
    p25 = prices[int(0.25 * (len(prices) - 1))]
    return max(0.0, p25 * battery.discharge_efficiency - battery.cycle_cost)


def optimize(
    slots: list[SlotInput],
    battery: BatteryParams,
    grid: GridParams,
    strategy: Strategy,
    *,
    now: datetime,
    soc_step_pct: float = 1.0,
) -> Plan:
    """Compute the cost-optimal battery schedule over `slots`."""
    plan = Plan(created=now, soc_start_kwh=battery.soc_kwh, soc_end_kwh=battery.soc_kwh)
    if not slots:
        return plan

    step = max(battery.capacity_kwh * soc_step_pct / 100.0, 0.01)
    soc0 = battery.soc_kwh
    lowest = min(battery.min_soc_kwh, soc0)
    highest = max(battery.max_soc_kwh, soc0)
    k_lo = -math.floor((soc0 - lowest) / step + EPS)
    k_hi = math.floor((highest - soc0) / step + EPS)
    n_states = k_hi - k_lo + 1
    i0 = -k_lo
    soc_of = [soc0 + (i + k_lo) * step for i in range(n_states)]

    tv = terminal_value(slots, battery)
    plan.terminal_value = tv

    # Per-slot cost of each delta (in steps).
    flows: list[dict[int, _Flow]] = []
    for slot in slots:
        up = math.floor(battery.max_charge_kw * slot.hours / step + EPS)
        down = math.floor(battery.max_discharge_kw * slot.hours / step + EPS)
        options: dict[int, _Flow] = {}
        for d in range(-down, up + 1):
            flow = slot_flow(slot, d * step, battery, grid, strategy)
            if flow is not None:
                options[d] = flow
        if 0 not in options:
            # Idle must always be possible; fall back to unconstrained flow.
            options[0] = slot_flow(slot, 0.0, battery, GridParams(1e9, 1e9), Strategy())  # type: ignore[assignment]
        flows.append(options)

    # Backward pass.
    n_slots = len(slots)
    value = [-max(0.0, soc_of[i] - battery.min_soc_kwh) * tv for i in range(n_states)]
    choice: list[list[int]] = [[0] * n_states for _ in range(n_slots)]
    # Cost-to-go after each slot: its slope is the value of stored energy,
    # which classifies "do nothing" slots (hold vs. let the ESS cover the load).
    after: list[list[float]] = [value] * n_slots
    for t in range(n_slots - 1, -1, -1):
        after[t] = value
        options = flows[t]
        new_value = [INF] * n_states
        row = choice[t]
        for i in range(n_states):
            soc = soc_of[i]
            floor_soc = min(battery.min_soc_kwh, soc) - EPS
            ceil_soc = max(battery.max_soc_kwh, soc) + EPS
            best = INF
            best_d = 0
            for d, flow in options.items():
                j = i + d
                if j < 0 or j >= n_states:
                    continue
                nxt = soc_of[j]
                if nxt < floor_soc or nxt > ceil_soc:
                    continue
                total = flow.cost + value[j]
                if total < best - 1e-12:
                    best = total
                    best_d = d
            new_value[i] = best
            row[i] = best_d
        value = new_value

    # Forward pass: reconstruct the path.
    i = i0
    total_cost = 0.0
    wear_cost = 0.0
    for t, slot in enumerate(slots):
        d = choice[t][i]
        flow = flows[t][d]
        soc_start = soc_of[i]
        i += d
        soc_end = soc_of[i]
        total_cost += flow.money
        wear_cost += flow.wear
        plan.slots.append(
            _slot_plan(slot, d * step, flow, soc_start, soc_end, step, battery, _marginal_values(after[t], i, step))
        )
    plan.total_cost = total_cost
    plan.wear_cost = wear_cost
    plan.baseline_cost = baseline_cost(slots)
    plan.soc_end_kwh = soc_of[i]
    return plan


def _marginal_values(cost_to_go: list[float], j: int, step: float) -> tuple[float | None, float | None]:
    """EUR per stored kWh of having one step less / one step more after a slot."""
    less = (cost_to_go[j - 1] - cost_to_go[j]) / step if j > 0 else None
    more = (cost_to_go[j] - cost_to_go[j + 1]) / step if j + 1 < len(cost_to_go) else None
    return less, more


def _slot_plan(
    slot: SlotInput,
    delta: float,
    flow: _Flow,
    soc_start: float,
    soc_end: float,
    step: float,
    battery: BatteryParams,
    marginal: tuple[float | None, float | None] = (None, None),
) -> SlotPlan:
    """Classify a DP decision into an action the controller can execute.

    A slot without planned battery power is executed as HOLD (no discharge)
    when the battery should not cover the load, and as SELF_CONSUME when it
    should but the SoC grid is too coarse to plan it (load below one SoC step).
    The decision compares what covering the load now is worth with what the
    stored energy is worth later (slope of the DP cost-to-go).
    """
    reason = ""
    if delta > EPS:
        if flow.grid_charged > 1e-3:
            action = Action.CHARGE_GRID
            reason = f"grid charge at {slot.import_price * 100:.1f} ct/kWh"
        else:
            action = Action.SELF_CONSUME
            reason = "store PV surplus"
    elif delta < -EPS:
        if flow.battery_exported > 1e-3:
            action = Action.DISCHARGE_GRID
            reason = f"sell battery at {slot.export_price * 100:.1f} ct/kWh"
        else:
            action = Action.SELF_CONSUME
            reason = "battery covers load"
    else:
        value_less, value_more = marginal
        can_discharge = soc_start - step >= battery.min_soc_kwh - EPS and value_less is not None
        can_charge = soc_start + step <= battery.max_soc_kwh + EPS and value_more is not None
        if flow.grid_import > MIN_FLOW_KWH:
            if not can_discharge:
                # Do not let the ESS go below our minimum (its own may be lower).
                action = Action.HOLD
                reason = "battery at minimum SoC"
            elif slot.import_price * battery.discharge_efficiency - battery.cycle_cost > value_less + EPS:  # type: ignore[operator]
                action = Action.SELF_CONSUME
                reason = "battery covers load"
            else:
                action = Action.HOLD
                reason = "save battery for higher prices"
        elif flow.grid_export > MIN_FLOW_KWH and can_charge:
            if value_more * battery.charge_efficiency > slot.export_price + EPS:  # type: ignore[operator]
                action = Action.SELF_CONSUME
                reason = "store PV surplus"
            else:
                action = Action.HOLD
                reason = "keep room for cheaper energy later"
        else:
            action = Action.SELF_CONSUME
            reason = "idle"
    if flow.curtailed > 1e-3 and slot.export_price < 0:
        reason = f"curtail PV (export {slot.export_price * 100:.1f} ct/kWh); " + reason

    capacity = battery.capacity_kwh or 1.0
    hours = slot.hours or 0.25
    return SlotPlan(
        start=slot.start,
        hours=slot.hours,
        action=action,
        battery_kw=delta / hours,
        grid_kw=(flow.grid_import - flow.grid_export) / hours,
        pv_kw=slot.pv_kwh / hours,
        load_kw=slot.load_kwh / hours,
        curtail_kw=flow.curtailed / hours,
        soc_start_pct=100.0 * soc_start / capacity,
        soc_end_pct=100.0 * soc_end / capacity,
        import_price=slot.import_price,
        export_price=slot.export_price,
        cost=flow.money,
        wear_cost=flow.wear,
        reason=reason,
    )
