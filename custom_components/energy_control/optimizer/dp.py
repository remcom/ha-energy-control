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
from dataclasses import astuple, dataclass
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
    overload: float = 0.0


def slot_flow(
    slot: SlotInput,
    delta_kwh: float,
    battery: BatteryParams,
    grid: GridParams,
    strategy: Strategy,
    *,
    allow_overload: bool = False,
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
        overload = max(0.0, grid_import - max_import)
        if overload > EPS and not allow_overload:
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
        if (not strategy.battery_to_grid or not slot.export_price_known) and battery_exported > EPS:
            continue

        money = grid_import * slot.import_price - grid_export * slot.export_price
        wear = max(-delta_kwh, 0.0) * battery.cycle_cost
        cost = money + wear + (grid_charged + battery_exported) * strategy.arbitrage_margin
        cost += overload * 1e6  # minimize unavoidable overload before monetary cost
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
                overload,
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
    for params in (battery, grid, strategy):
        if any(not math.isfinite(value) for value in astuple(params)):
            raise ValueError("Nonfinite planning parameter")
    if (
        battery.capacity_kwh <= 0
        or not 0 < battery.charge_efficiency <= 1
        or not 0 < battery.discharge_efficiency <= 1
        or not 0 <= battery.min_soc_kwh <= battery.max_soc_kwh <= battery.capacity_kwh
        or not 0 <= battery.soc_kwh <= battery.capacity_kwh
        or min(battery.max_charge_kw, battery.max_discharge_kw, grid.max_import_kw, grid.max_export_kw) < 0
    ):
        raise ValueError("Invalid battery or grid limits")
    for slot in slots:
        values = (slot.hours, slot.import_price, slot.export_price, slot.pv_kwh, slot.load_kwh)
        if any(not math.isfinite(value) for value in values) or slot.hours <= 0 or min(slot.pv_kwh, slot.load_kwh) < 0:
            raise ValueError("Invalid forecast interval or nonfinite forecast")
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
    soc_of = [soc0 + (i + k_lo) * step for i in range(n_states)]

    tv = terminal_value(slots, battery)
    plan.terminal_value = tv

    # Cache state-independent candidates, including exact natural self
    # consumption and grid assistance even when smaller than one SoC step.
    flows: list[dict[float, _Flow]] = []
    for slot in slots:
        up = math.floor(battery.max_charge_kw * slot.hours / step + EPS)
        down = math.floor(battery.max_discharge_kw * slot.hours / step + EPS)
        net = slot.load_kwh - slot.pv_kwh
        natural = -net / battery.discharge_efficiency if net >= 0 else -net * battery.charge_efficiency
        assistance = -(net - grid.max_import_kw * slot.hours) / battery.discharge_efficiency
        deltas = {d * step for d in range(-down, up + 1)}
        deltas.update((natural, assistance))
        options: dict[float, _Flow] = {}
        for delta in sorted(deltas):
            if not -battery.max_discharge_kw * slot.hours - EPS <= delta <= battery.max_charge_kw * slot.hours + EPS:
                continue
            flow = slot_flow(slot, delta, battery, grid, strategy, allow_overload=True)
            if flow is not None:
                options[delta] = flow
        flows.append(options)

    # Backward pass uses interpolation for exact sub-step transitions too.
    value = [-max(0.0, soc - battery.min_soc_kwh) * tv for soc in soc_of]
    after: list[list[float]] = [value] * len(slots)
    for t in range(len(slots) - 1, -1, -1):
        after[t] = value
        new_value = []
        for soc in soc_of:
            floor_soc = min(battery.min_soc_kwh, soc) - EPS
            ceil_soc = max(battery.max_soc_kwh, soc) + EPS
            best = INF
            for delta, flow in flows[t].items():
                nxt = soc + delta
                if nxt < floor_soc or nxt > ceil_soc:
                    continue
                total = flow.cost + _interpolate(value, nxt, soc_of, step)
                if total < best:
                    best = total
            new_value.append(best)
        value = new_value

    # Reconstruct at the actual continuous SoC, evaluating exact natural flows
    # against interpolated future values. Sub-step energy must propagate into
    # every subsequent slot; changing displayed power alone violates conservation.
    soc = soc0
    for t, slot in enumerate(slots):
        floor_soc = min(soc, battery.min_soc_kwh)
        ceil_soc = max(soc, battery.max_soc_kwh)
        lo = max(floor_soc - soc, -battery.max_discharge_kw * slot.hours)
        hi = min(ceil_soc - soc, battery.max_charge_kw * slot.hours)
        net = slot.load_kwh - slot.pv_kwh
        natural = -net / battery.discharge_efficiency if net >= 0 else -net * battery.charge_efficiency
        assistance = -(net - grid.max_import_kw * slot.hours) / battery.discharge_efficiency
        deltas = {0.0, lo, hi, max(lo, min(hi, natural)), max(lo, min(hi, assistance))}
        deltas.update(delta for delta in flows[t] if lo - EPS <= delta <= hi + EPS)
        best = INF
        selected: tuple[float, _Flow] | None = None
        for delta in sorted(deltas):
            if delta < lo - EPS or delta > hi + EPS:
                continue
            flow = slot_flow(slot, delta, battery, grid, strategy, allow_overload=True)
            if flow is None:
                continue
            future = _interpolate(after[t], soc + delta, soc_of, step)
            objective = flow.cost + future
            if objective < best - EPS:
                best = objective
                selected = delta, flow
        if selected is None:
            raise ValueError("No executable battery trajectory")
        delta, flow = selected
        plan.slots.append(_slot_plan(slot, delta, flow, soc, soc + delta, step, battery))
        soc += delta
    plan.total_cost = sum(slot.cost for slot in plan.slots)
    plan.wear_cost = sum(slot.wear_cost for slot in plan.slots)
    plan.baseline_cost = baseline_cost(slots)
    plan.soc_end_kwh = soc
    return plan


def _interpolate(values: list[float], soc: float, states: list[float], step: float) -> float:
    position = max(0.0, min(len(states) - 1.0, (soc - states[0]) / step))
    low = math.floor(position)
    high = min(low + 1, len(states) - 1)
    weight = position - low
    return values[low] * (1 - weight) + values[high] * weight


def _slot_plan(
    slot: SlotInput,
    delta: float,
    flow: _Flow,
    soc_start: float,
    soc_end: float,
    step: float,
    battery: BatteryParams,
) -> SlotPlan:
    """Classify an evaluated, continuous stored-energy change."""
    reason = ""
    # Battery/grid power as the ESS will execute it (differs from the DP flow
    # only when the battery covers a load smaller than one SoC step).
    battery_kwh = delta
    grid_kwh = flow.grid_import - flow.grid_export
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
        if flow.grid_import > MIN_FLOW_KWH:
            action = Action.HOLD
            reason = (
                "battery at minimum SoC" if soc_start <= battery.min_soc_kwh + EPS else "save battery for higher prices"
            )
        elif flow.grid_export > MIN_FLOW_KWH:
            action = Action.HOLD
            reason = "keep room for cheaper energy later"
        else:
            action = Action.HOLD
            reason = "idle"
    if flow.overload > EPS:
        reason = f"unavoidable grid import exceeds limit by {flow.overload / slot.hours:.3f} kW; " + reason
    if flow.curtailed > 1e-3 and slot.export_price < 0:
        reason = f"curtail PV (export {slot.export_price * 100:.1f} ct/kWh); " + reason

    capacity = battery.capacity_kwh or 1.0
    hours = slot.hours or 0.25
    return SlotPlan(
        start=slot.start,
        hours=slot.hours,
        action=action,
        battery_kw=battery_kwh / hours,
        grid_kw=grid_kwh / hours,
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
        grid_violation_kw=flow.overload / hours,
        export_price_known=slot.export_price_known,
    )
