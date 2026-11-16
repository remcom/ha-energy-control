"""PV forecast parsing. Pure Python: works on entity attributes.

Supported layouts (auto-detected):
- Solcast: ``detailedForecast`` [{period_start, pv_estimate|pv_estimate10|pv_estimate90}] (kW, 30 min)
- Forecast.Solar / Open-Meteo Solar Forecast: ``wh_period`` {iso: Wh}
- Generic: ``forecast`` [{datetime|period_start|start, value|energy|power}]
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .util import parse_dt, to_float


@dataclass(frozen=True, slots=True)
class EnergyInterval:
    """Energy (kWh) produced in [start, start + minutes)."""

    start: datetime
    minutes: int
    kwh: float

    @property
    def end(self) -> datetime:
        """End of interval."""
        return self.start + timedelta(minutes=self.minutes)


def _infer_minutes(starts: list[datetime], default: int) -> list[int]:
    out = []
    for i, start in enumerate(starts):
        if i + 1 < len(starts):
            minutes = int((starts[i + 1] - start).total_seconds() // 60)
        else:
            minutes = out[-1] if out else default
        out.append(minutes if 0 < minutes <= 180 else default)
    return out


def parse_solcast(attributes: Mapping[str, Any], estimate: str = "pv_estimate") -> list[EnergyInterval]:
    """Solcast detailedForecast: kW averages per period."""
    rows = attributes.get("detailedForecast")
    if not isinstance(rows, list):
        return []
    parsed = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        start = parse_dt(row.get("period_start"))
        kw = to_float(row.get(estimate, row.get("pv_estimate")))
        if start is None or kw is None:
            continue
        parsed.append((start, kw))
    parsed.sort(key=lambda r: r[0])
    minutes = _infer_minutes([p[0] for p in parsed], 30)
    return [EnergyInterval(s, m, kw * m / 60) for (s, kw), m in zip(parsed, minutes, strict=True)]


def parse_wh_period(attributes: Mapping[str, Any]) -> list[EnergyInterval]:
    """Forecast.Solar / Open-Meteo: {iso_start: Wh} per period."""
    periods = attributes.get("wh_period")
    if not isinstance(periods, Mapping):
        return []
    parsed = sorted(
        (dt, wh)
        for key, raw in periods.items()
        if (dt := parse_dt(key)) is not None and (wh := to_float(raw)) is not None
    )
    minutes = _infer_minutes([p[0] for p in parsed], 60)
    return [EnergyInterval(s, m, wh / 1000) for (s, wh), m in zip(parsed, minutes, strict=True)]


def parse_generic(attributes: Mapping[str, Any]) -> list[EnergyInterval]:
    """Generic forecast list of {start, value} with value in kWh per period."""
    rows = attributes.get("forecast")
    if not isinstance(rows, list):
        return []
    parsed = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        start = next((parse_dt(row[k]) for k in ("period_start", "datetime", "start") if k in row), None)
        value = next((to_float(row[k]) for k in ("energy", "value", "kwh") if k in row), None)
        if start is None or value is None:
            continue
        parsed.append((start, value))
    parsed.sort(key=lambda r: r[0])
    minutes = _infer_minutes([p[0] for p in parsed], 60)
    return [EnergyInterval(s, m, v) for (s, v), m in zip(parsed, minutes, strict=True)]


def parse_pv_attributes(attributes: Mapping[str, Any], estimate: str = "pv_estimate") -> list[EnergyInterval]:
    """Auto-detect the forecast layout."""
    return parse_solcast(attributes, estimate) or parse_wh_period(attributes) or parse_generic(attributes)


def energy_between(intervals: Iterable[EnergyInterval], start: datetime, end: datetime) -> float:
    """kWh in [start, end), spreading each interval's energy evenly."""
    total = 0.0
    for interval in intervals:
        overlap = (min(end, interval.end) - max(start, interval.start)).total_seconds()
        if overlap > 0:
            total += interval.kwh * overlap / (interval.minutes * 60)
    return total
