"""Cheapest time window for a flexible load. Pure Python."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..optimizer.model import SlotPlan


@dataclass(frozen=True, slots=True)
class Window:
    """A chosen run window and its expected cost."""

    start: datetime
    end: datetime
    cost: float  # EUR for the extra energy
    energy_kwh: float
    price_estimated: bool  # some slots use predicted prices
    slots: tuple[tuple[datetime, datetime], ...] = ()  # runs (non-contiguous mode)

    @property
    def average_price(self) -> float:
        """EUR/kWh."""
        return self.cost / self.energy_kwh if self.energy_kwh else 0.0


def marginal_cost(slot: SlotPlan, extra_kwh: float) -> float:
    """Cost of extra consumption in a slot, given the plan.

    PV the plan exports anyway is used first (it only loses the export
    price); the rest is imported. The battery schedule is left as planned.
    """
    surplus = max(0.0, -slot.grid_kw) * slot.hours
    from_surplus = min(extra_kwh, surplus)
    return from_surplus * slot.export_price + (extra_kwh - from_surplus) * slot.import_price


def _end(slot: SlotPlan) -> datetime:
    return slot.start + timedelta(hours=slot.hours)


def _cost_between(slots: list[SlotPlan], start: datetime, end: datetime, power_kw: float) -> tuple[float, bool]:
    cost = 0.0
    estimated = False
    for slot in slots:
        overlap = (min(end, _end(slot)) - max(start, slot.start)).total_seconds() / 3600
        if overlap > 0:
            cost += marginal_cost(slot, power_kw * overlap)
            estimated = estimated or slot.price_estimated
    return cost, estimated


def cheapest_window(
    slots: list[SlotPlan],
    duration: timedelta,
    power_kw: float,
    *,
    earliest: datetime,
    latest_end: datetime | None = None,
    contiguous: bool = True,
) -> Window | None:
    """Cheapest window of `duration` within [earliest, latest_end], or None.

    Contiguous windows start at `earliest` or a slot boundary. Non-contiguous
    mode picks the cheapest slots that add up to the duration (EV charging).
    """
    if not slots or duration <= timedelta(0) or power_kw <= 0:
        return None
    horizon_end = _end(slots[-1])
    latest_end = min(latest_end or horizon_end, horizon_end)
    earliest = max(earliest, slots[0].start)
    if latest_end - earliest < duration:
        return None
    energy = power_kw * duration.total_seconds() / 3600

    if contiguous:
        starts = sorted({earliest, *(s.start for s in slots if earliest < s.start <= latest_end - duration)})
        best: Window | None = None
        for start in starts:
            cost, estimated = _cost_between(slots, start, start + duration, power_kw)
            if best is None or cost < best.cost - 1e-9:
                best = Window(start, start + duration, cost, energy, estimated)
        return best

    # Non-contiguous: rank slot pieces inside the window by marginal price.
    pieces = []
    for slot in slots:
        start, end = max(slot.start, earliest), min(_end(slot), latest_end)
        hours = (end - start).total_seconds() / 3600
        if hours > 1e-6:
            pieces.append((marginal_cost(slot, power_kw * hours) / (power_kw * hours), start, end, slot))
    pieces.sort(key=lambda p: (p[0], p[1]))
    remaining = duration
    chosen = []
    for _, start, end, slot in pieces:
        if remaining <= timedelta(0):
            break
        take = min(end - start, remaining)
        chosen.append((start, start + take, slot))
        remaining -= take
    chosen.sort(key=lambda c: c[0])
    cost = sum(marginal_cost(s, power_kw * (b - a).total_seconds() / 3600) for a, b, s in chosen)
    runs: list[tuple[datetime, datetime]] = []
    for a, b, _ in chosen:
        if runs and runs[-1][1] == a:
            runs[-1] = (runs[-1][0], b)
        else:
            runs.append((a, b))
    return Window(chosen[0][0], chosen[-1][1], cost, energy, any(s.price_estimated for *_, s in chosen), tuple(runs))
