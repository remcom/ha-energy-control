"""EV charging schedule. Pure Python."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, tzinfo
from enum import StrEnum

from ..optimizer.model import SlotPlan
from .window import cheapest_window

QUARTER = timedelta(minutes=15)


class EvMode(StrEnum):
    """User choice for EV charging."""

    MANUAL = "manual"  # Energy Control does not touch the charger
    SMART = "smart"  # cheapest quarters before the ready-by time
    NOW = "now"  # charge immediately


def deadline(now: datetime, ready_by: time, tz: tzinfo) -> datetime:
    """Next local `ready_by` after now."""
    local = now.astimezone(tz)
    target = datetime.combine(local.date(), ready_by, tz)
    if target <= local:
        target = datetime.combine(local.date() + timedelta(days=1), ready_by, tz)
    return target


def schedule_ev(
    slots: list[SlotPlan], need_kwh: float, power_kw: float, *, now: datetime, ready_by: datetime
) -> set[datetime]:
    """Quarter starts to charge in: the cheapest ones before `ready_by`.

    When the energy does not fit before the deadline, charge from now on so
    the car gets as much as possible.
    """
    if need_kwh <= 0 or power_kw <= 0 or not slots:
        return set()
    quarters = math.ceil(need_kwh / (power_kw * 0.25) - 1e-9)
    window = cheapest_window(slots, QUARTER * quarters, power_kw, earliest=now, latest_end=ready_by, contiguous=False)
    if window is None:
        return {s.start for s in slots if s.start < ready_by or s.start <= now}
    starts: set[datetime] = set()
    for run_start, run_end in window.slots:
        for s in slots:
            end = s.start + timedelta(hours=s.hours)
            if s.start < run_end and end > run_start:
                starts.add(s.start)
    return starts


def ev_energy(slots: list[SlotPlan], charging: set[datetime], power_kw: float) -> list[float]:
    """Expected EV energy (kWh) per slot."""
    return [power_kw * s.hours if s.start in charging else 0.0 for s in slots]
