"""Assemble optimizer slot inputs from provider data. Pure Python."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
        end = min(end, point.end)
        hours = (end - start).total_seconds() / 3600
        export_price = model.export_price(point, start.astimezone(tz).date())
        if hours > 1e-3:
            pv_kwh = energy_between(pv, start, end) * pv_scale
            load_kwh = load.at(start) * hours
            slots.append(
                SlotInput(
                    start=start,
                    hours=hours,
                    import_price=model.import_price(point),
                    export_price=export_price if export_price is not None else 0.0,
                    export_price_known=export_price is not None,
                    pv_kwh=pv_kwh,
                    load_kwh=load_kwh,
                    pv_forecast_kwh=pv_kwh,
                    load_forecast_kwh=load_kwh,
                    price_estimated=point.estimated,
                )
            )
        start = end
        slot_end = end + timedelta(minutes=SLOT_MINUTES)
    return slots


@dataclass(frozen=True, slots=True)
class Corrections:
    """Measured deviations from the forecast, applied to the near-term slots.

    Factors compare measurements with the *uncorrected* forecast, so applying
    a correction never feeds back into the next measurement of it.
    """

    load_factor: float = 1.0  # recent actual / forecast load (12 h)
    pv_factor: float = 1.0  # recent actual / forecast PV (last hour)
    live_load_kw: float | None = None  # average load in the running slot
    live_pv_kw: float | None = None  # average PV in the running slot


LOAD_DECAY_H = 2.0  # recent load deviation fades out over this horizon
PV_DECAY_H = 3.0  # cloud cover persists for a few hours, not a day


def apply_corrections(slots: list[SlotInput], now: datetime, corr: Corrections) -> list[SlotInput]:
    """Blend live measurements into the running slot and decay factors over time.

    - Running slot: the live average replaces the forecast (it is what is
      happening right now); the next slot gets half of it.
    - Later slots: the recent factor, fading out linearly.
    """
    out: list[SlotInput] = []
    for slot in slots:
        ahead_h = max(0.0, (slot.start - now).total_seconds() / 3600)
        running = slot.start <= now + timedelta(seconds=1)
        load_kwh = slot.load_kwh * (1 + (corr.load_factor - 1) * max(0.0, 1 - ahead_h / LOAD_DECAY_H))
        pv_kwh = slot.pv_kwh * (1 + (corr.pv_factor - 1) * max(0.0, 1 - ahead_h / PV_DECAY_H))
        live_weight = 1.0 if running else 0.5 if ahead_h < SLOT_MINUTES / 60 + 1e-6 else 0.0
        if live_weight and corr.live_load_kw is not None:
            load_kwh = live_weight * corr.live_load_kw * slot.hours + (1 - live_weight) * load_kwh
        # PV only when the sun is up per forecast: a live reading at dusk must
        # not create production in a slot the forecast says is dark.
        if live_weight and corr.live_pv_kw is not None and slot.pv_kwh > 0:
            pv_kwh = live_weight * corr.live_pv_kw * slot.hours + (1 - live_weight) * pv_kwh
        out.append(replace(slot, load_kwh=max(0.0, load_kwh), pv_kwh=max(0.0, pv_kwh)))
    return out
