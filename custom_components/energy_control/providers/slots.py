"""Assemble optimizer slot inputs from provider data. Pure Python."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from ..optimizer.model import SlotInput
from .load import LoadProfile
from .prices import PriceModel, PricePoint, price_at
from .solar import EnergyInterval, energy_between

SLOT_MINUTES = 15


def floor_slot(when: datetime, minutes: int = SLOT_MINUTES) -> datetime:
    """Start of the slot containing `when`."""
    return when.replace(minute=when.minute - when.minute % minutes, second=0, microsecond=0)


def build_slots(
    now: datetime,
    prices: list[PricePoint],
    pv: list[EnergyInterval],
    load: LoadProfile,
    model: PriceModel,
    tz: tzinfo,
    horizon_hours: float = 48,
    pv_scale: float = 1.0,
) -> list[SlotInput]:
    """15-minute slots from now until the end of known prices (max horizon)."""
    if not prices:
        return []
    limit = min(prices[-1].end, now + timedelta(hours=horizon_hours))
    slots: list[SlotInput] = []
    start = now
    slot_end = floor_slot(now) + timedelta(minutes=SLOT_MINUTES)
    while start < limit:
        end = min(slot_end, limit)
        hours = (end - start).total_seconds() / 3600
        point = price_at(prices, start)
        if point is None:
            break  # gap in price data: stop the horizon here
        if hours > 1e-3:
            slots.append(
                SlotInput(
                    start=start,
                    hours=hours,
                    import_price=model.import_price(point),
                    export_price=model.export_price(point, start.astimezone(tz).date()),
                    pv_kwh=energy_between(pv, start, end) * pv_scale,
                    load_kwh=load.at(start) * hours,
                )
            )
        start = end
        slot_end = end + timedelta(minutes=SLOT_MINUTES)
    return slots
