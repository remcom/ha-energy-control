"""Heat pump scheduling via SG Ready / EVU block. Pure Python.

SG Ready states (what the heat pump does with them is up to its controller):

- BLOCK   (state 1, "EVU"): utility lock, the compressor stays off.
- NORMAL  (state 2): normal operation.
- COMFORT (state 3, "recommended on"): raise setpoints, buffer heat.
- BOOST   (state 4, "forced on"): run now (not every heat pump supports it).

Blocking only shifts heat demand: after a block the heat pump catches up
(the "rebound"), slightly less efficiently. A block is worth it only when the
blocked quarters are clearly more expensive than the quarters where the
rebound lands. How long the house may go without heat depends on the outdoor
temperature, so the allowed block length shrinks as it gets colder and drops
to zero at the configured minimum temperature.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from ..optimizer.model import SlotPlan
from .window import marginal_cost

QUARTER = timedelta(minutes=15)


class HpMode(StrEnum):
    """SG Ready state."""

    NORMAL = "normal"
    BLOCK = "block"
    COMFORT = "comfort"
    BOOST = "boost"


@dataclass(frozen=True, slots=True)
class HeatPumpParams:
    """Comfort and safety limits."""

    max_block_minutes: float = 120.0  # at or above full_temp (EVU rules allow 2 h)
    block_min_temp: float = 2.0  # never block at or below this outdoor temperature
    block_full_temp: float = 12.0  # full block length from this temperature
    max_block_hours_per_day: float = 6.0  # at full_temp; scaled like the block length
    rebound_penalty: float = 0.05  # catching up costs this much more energy (lower COP)
    min_saving_eur: float = 0.03  # a block must save at least this much
    comfort: bool = True  # use state 3 when energy is cheap
    boost: bool = False  # use state 4 (only if the heat pump supports it)
    comfort_extra: float = 0.25  # extra consumption while in comfort mode
    boost_extra: float = 0.5  # extra consumption while boosting


def block_allowance(temp: float | None, params: HeatPumpParams) -> float:
    """Fraction (0-1) of the maximum block length allowed at `temp`."""
    if temp is None:
        return 0.0  # unknown temperature: do not risk a cold house
    span = params.block_full_temp - params.block_min_temp
    if span <= 0:
        return 1.0 if temp > params.block_min_temp else 0.0
    return max(0.0, min(1.0, (temp - params.block_min_temp) / span))


def max_block(temp: float | None, params: HeatPumpParams) -> timedelta:
    """Longest allowed block at `temp`, in whole quarters (0 below 30 min)."""
    minutes = params.max_block_minutes * block_allowance(temp, params)
    quarters = int(minutes // 15)
    return timedelta(minutes=15 * quarters) if quarters >= 2 else timedelta(0)


@dataclass(frozen=True, slots=True)
class HpSlot:
    """Inputs for one plan quarter."""

    plan: SlotPlan
    power_kw: float  # expected heat pump power in normal operation
    temp: float | None  # expected outdoor temperature


def _cost(slot: HpSlot, kwh: float) -> float:
    return marginal_cost(slot.plan, kwh)


def schedule_heat_pump(
    slots: list[HpSlot],
    params: HeatPumpParams,
    *,
    now: datetime,
    blocked_since: datetime | None = None,
    blocked_today_h: float = 0.0,
    recovery_until: datetime | None = None,
) -> list[HpMode]:
    """Mode per slot: greedy most-profitable blocks, then comfort/boost.

    `blocked_since`, `blocked_today_h` and `recovery_until` carry the live
    state, so a replan never extends a running block past its limit.
    """
    n = len(slots)
    modes = [HpMode.NORMAL] * n
    if n == 0:
        return modes
    locked = [False] * n  # recovery or comfort/boost: not blockable
    for i, s in enumerate(slots):
        if recovery_until is not None and s.plan.start < recovery_until:
            locked[i] = True
    budget = timedelta(hours=params.max_block_hours_per_day * block_allowance(slots[0].temp, params))
    budget -= timedelta(hours=blocked_today_h)

    # A block already running continues only within its remaining length.
    if blocked_since is not None:
        remaining = max_block(slots[0].temp, params) - (now - blocked_since)
        i = 0
        while i < n and remaining > timedelta(0) and _worth_continuing(slots, i, params):
            modes[i] = HpMode.BLOCK
            remaining -= timedelta(hours=slots[i].plan.hours)
            budget -= timedelta(hours=slots[i].plan.hours)
            i += 1
        # Recovery after the running block: as long as the whole block.
        recovery = (now - blocked_since).total_seconds() / 3600 + sum(slots[k].plan.hours for k in range(i))
        k = i
        while k < n and recovery > 1e-9:
            locked[k] = True
            recovery -= slots[k].plan.hours
            k += 1

    while budget >= timedelta(minutes=30):
        best: tuple[float, int, int] | None = None
        for i in range(n):
            limit = min(max_block(slots[i].temp, params), budget)
            length = 0
            energy = 0.0
            value = 0.0
            j = i
            while j < n and not locked[j] and modes[j] == HpMode.NORMAL and slots[j].temp is not None:
                if max_block(slots[j].temp, params) < limit:
                    limit = max_block(slots[j].temp, params)
                step = timedelta(hours=slots[j].plan.hours)
                if timedelta(hours=length) + step > limit:
                    break
                kwh = slots[j].power_kw * slots[j].plan.hours
                energy += kwh
                value += _cost(slots[j], kwh)
                length += slots[j].plan.hours
                j += 1
                if length >= 0.5 - 1e-9 and energy > 0:
                    saving = value - _rebound_cost(slots, j, energy, length, params)
                    if saving >= params.min_saving_eur and (best is None or saving > best[0]):
                        best = (saving, i, j)
        if best is None:
            break
        _, i, j = best
        for k in range(i, j):
            modes[k] = HpMode.BLOCK
            budget -= timedelta(hours=slots[k].plan.hours)
        # Recovery: as long as the block, during which no new block starts.
        recovery = sum(slots[k].plan.hours for k in range(i, j))
        k = j
        while k < n and recovery > 1e-9:
            locked[k] = True
            recovery -= slots[k].plan.hours
            k += 1
        if i > 0:
            locked[i - 1] = True  # keep blocks apart

    _comfort_and_boost(slots, modes, params)
    return modes


def _worth_continuing(slots: list[HpSlot], i: int, params: HeatPumpParams) -> bool:
    """Continue a running block while this quarter is dearer than the next hour."""
    later = slots[i + 1 : i + 5]
    if not later or slots[i].temp is None or block_allowance(slots[i].temp, params) <= 0:
        return False
    avg_later = sum(s.plan.import_price for s in later) / len(later)
    return slots[i].plan.import_price > avg_later + 0.01


def _rebound_cost(slots: list[HpSlot], start: int, energy: float, hours: float, params: HeatPumpParams) -> float:
    """Cost of catching up `energy` in the quarters after a block of `hours`."""
    window = []
    covered = 0.0
    k = start
    while k < len(slots) and covered < hours - 1e-9:
        window.append(slots[k])
        covered += slots[k].plan.hours
        k += 1
    if not window:
        return float("inf")  # no room to catch up within the plan
    extra = energy * (1 + params.rebound_penalty)
    total_h = sum(s.plan.hours for s in window)
    # The plan already contains the normal heat pump load: the rebound is
    # extra consumption on top of it.
    return sum(_cost(s, extra * s.plan.hours / total_h) for s in window)


def _comfort_and_boost(slots: list[HpSlot], modes: list[HpMode], params: HeatPumpParams) -> None:
    """Buffer heat when energy is nearly free (exported PV, cheap or negative prices)."""
    if not (params.comfort or params.boost):
        return
    prices = sorted(s.plan.import_price for s in slots[:96])
    if not prices:
        return
    cheap = prices[int(0.15 * (len(prices) - 1))]
    median = prices[len(prices) // 2]
    for i, s in enumerate(slots):
        if modes[i] != HpMode.NORMAL or (i > 0 and modes[i - 1] == HpMode.BLOCK):
            continue
        p = s.plan
        exporting = p.grid_kw < -max(0.3, s.power_kw)
        if params.boost and (p.import_price < 0 or (exporting and p.export_price <= 0)):
            modes[i] = HpMode.BOOST
        elif params.comfort and (exporting or (p.import_price <= cheap and p.import_price < median - 0.05)):
            modes[i] = HpMode.COMFORT


def load_with_heat_pump(slots: list[HpSlot], modes: list[HpMode], params: HeatPumpParams) -> list[float]:
    """Expected heat pump energy (kWh) per slot under the schedule."""
    energy = [s.power_kw * s.plan.hours for s in slots]
    i = 0
    n = len(slots)
    while i < n:
        if modes[i] == HpMode.BLOCK:
            j = i
            shifted = 0.0
            while j < n and modes[j] == HpMode.BLOCK:
                shifted += energy[j]
                energy[j] = 0.0
                j += 1
            hours = sum(slots[k].plan.hours for k in range(i, j))
            # Spread the rebound over a window as long as the block.
            k, covered = j, 0.0
            window = []
            while k < n and covered < hours - 1e-9:
                window.append(k)
                covered += slots[k].plan.hours
                k += 1
            for k in window:
                energy[k] += shifted * (1 + params.rebound_penalty) * slots[k].plan.hours / max(covered, 1e-9)
            i = j
        else:
            if modes[i] == HpMode.COMFORT:
                energy[i] *= 1 + params.comfort_extra
            elif modes[i] == HpMode.BOOST:
                energy[i] *= 1 + params.boost_extra
            i += 1
    return energy
