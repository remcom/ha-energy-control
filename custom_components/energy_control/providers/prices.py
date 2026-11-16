"""Price forecast parsing. Pure Python: works on entity attributes.

Supported attribute layouts (auto-detected):
- tibber_prices: ``outlook_quarters`` {start, minutes, prices[], energy[]}
- Nord Pool (custom): ``raw_today`` / ``raw_tomorrow`` [{start, end, value}]
- ENTSO-e / EnergyZero / generic: ``prices``, ``prices_today``, ``prices_tomorrow``,
  ``forecast``, ``data`` lists of {start|startsAt|from|time|period_start, price|value|total}
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from .util import parse_dt, to_float

TIME_KEYS = ("start", "startsAt", "starts_at", "from", "time", "period_start", "start_time", "datetime")
VALUE_KEYS = ("price", "value", "total", "price_total", "electricity_price")
SPOT_KEYS = ("energy", "spot", "energy_price", "market_price")
LIST_KEYS = (
    "prices_today",
    "prices_tomorrow",
    "raw_today",
    "raw_tomorrow",
    "prices",
    "today",
    "tomorrow",
    "forecast",
    "data",
)


@dataclass(frozen=True, slots=True)
class PricePoint:
    """Price for an interval, EUR/kWh."""

    start: datetime
    minutes: int
    price: float  # as published by the source (scaled to EUR/kWh)
    spot: float | None = None  # energy/spot part if the source provides it

    @property
    def end(self) -> datetime:
        """End of the interval."""
        return self.start + timedelta(minutes=self.minutes)


class ExportMode(StrEnum):
    """How export (feed-in) is compensated."""

    NETTING = "netting"  # export = import price (NL saldering until 2027)
    SPOT = "spot"  # export = spot x factor - fee
    FIXED = "fixed"  # fixed EUR/kWh


@dataclass(frozen=True, slots=True)
class PriceModel:
    """Turns source prices into import / export prices (EUR/kWh)."""

    includes_taxes: bool = True
    markup: float = 0.0  # EUR/kWh supplier markup (spot-only sources)
    energy_tax: float = 0.0  # EUR/kWh (spot-only sources)
    vat: float = 0.21  # (spot-only sources)
    export_mode: ExportMode = ExportMode.NETTING
    netting_end: date | None = None  # after this date NETTING falls back to SPOT
    export_factor: float = 1.0
    export_fee: float = 0.0  # EUR/kWh subtracted from spot export price
    export_fixed: float = 0.0

    def import_price(self, point: PricePoint) -> float:
        """All-in import price."""
        if self.includes_taxes:
            return point.price
        return (point.price + self.markup + self.energy_tax) * (1 + self.vat)

    def export_price(self, point: PricePoint, local_date: date) -> float:
        """Export compensation for the interval."""
        mode = self.export_mode
        if mode == ExportMode.NETTING and self.netting_end and local_date >= self.netting_end:
            mode = ExportMode.SPOT
        if mode == ExportMode.NETTING:
            return self.import_price(point)
        if mode == ExportMode.FIXED:
            return self.export_fixed
        spot = point.spot if point.spot is not None else point.price
        return spot * self.export_factor - self.export_fee


def unit_scale(unit: str | None) -> float:
    """Factor to convert a price unit to EUR/kWh."""
    if not unit:
        return 1.0
    u = unit.lower().replace(" ", "")
    scale = 1.0
    if u.startswith(("ct", "c/", "cent", "¢")):
        scale = 0.01
    if u.endswith("/mwh"):
        scale /= 1000
    return scale


def _parse_quarters(block: Mapping[str, Any], scale: float) -> list[PricePoint]:
    start = parse_dt(block.get("start"))
    prices = block.get("prices") or []
    if start is None or not isinstance(prices, list):
        return []
    minutes = int(block.get("minutes") or 15)
    spots = block.get("energy") or []
    points: list[PricePoint] = []
    for i, raw in enumerate(prices):
        value = to_float(raw)
        if value is None:
            continue
        spot = to_float(spots[i]) if i < len(spots) else None
        points.append(
            PricePoint(
                start=start + timedelta(minutes=minutes * i),
                minutes=minutes,
                price=value * scale,
                spot=spot * scale if spot is not None else None,
            )
        )
    return points


def _parse_list(items: Iterable[Any], scale: float) -> list[PricePoint]:
    raw: list[tuple[datetime, float, float | None, datetime | None]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        start = next((parse_dt(item[k]) for k in TIME_KEYS if k in item), None)
        value = next((to_float(item[k]) for k in VALUE_KEYS if k in item), None)
        if start is None or value is None:
            continue
        spot = next((to_float(item[k]) for k in SPOT_KEYS if k in item), None)
        end = parse_dt(item.get("end") or item.get("endsAt") or item.get("till"))
        raw.append((start, value, spot, end))
    raw.sort(key=lambda r: r[0])
    points: list[PricePoint] = []
    for i, (start, value, spot, end) in enumerate(raw):
        if end is not None:
            minutes = int((end - start).total_seconds() // 60)
        elif i + 1 < len(raw):
            minutes = int((raw[i + 1][0] - start).total_seconds() // 60)
        elif points:
            minutes = points[-1].minutes
        else:
            minutes = 60
        if minutes <= 0 or minutes > 1440:
            minutes = 60
        points.append(PricePoint(start, minutes, value * scale, spot * scale if spot is not None else None))
    return points


def parse_price_attributes(attributes: Mapping[str, Any], unit: str | None = None) -> list[PricePoint]:
    """Extract a sorted, de-duplicated price forecast from entity attributes."""
    scale = unit_scale(unit or attributes.get("unit_of_measurement"))
    points: list[PricePoint] = []

    quarters = attributes.get("outlook_quarters")
    if isinstance(quarters, Mapping):
        points = _parse_quarters(quarters, scale)

    if not points:
        for key in LIST_KEYS:
            items = attributes.get(key)
            if isinstance(items, list):
                points.extend(_parse_list(items, scale))

    unique: dict[datetime, PricePoint] = {}
    for point in points:
        unique[point.start] = point
    # Sources can publish several resolutions (e.g. hourly and quarter-hour
    # lists). Shorten an interval that runs into the next one so intervals
    # never overlap: price_at relies on that.
    result: list[PricePoint] = []
    for start in sorted(unique):
        point = unique[start]
        if result and result[-1].end > start:
            prev = result[-1]
            result[-1] = replace(prev, minutes=int((start - prev.start).total_seconds() // 60))
        result.append(point)
    return result


def price_at(points: list[PricePoint], when: datetime) -> PricePoint | None:
    """Price interval covering `when` (points must be sorted)."""
    lo, hi = 0, len(points) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        point = points[mid]
        if when < point.start:
            hi = mid - 1
        elif when >= point.end:
            lo = mid + 1
        else:
            return point
    return None
