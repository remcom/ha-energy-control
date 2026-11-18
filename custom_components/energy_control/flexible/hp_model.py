"""Heat pump consumption model from history. Pure Python.

Heating demand follows the outdoor temperature, but the building's thermal
mass averages it over about a day, and the heat pump's own controller adds a
daily rhythm (night setback, hot water, defrost). So the model has two parts:

    power(t) = level(daily outdoor temperature) x shape(hour of day)

- level: a straight line fitted on *daily* averages,
  max(0, a + b x (18 - T_day)). The intercept is not clamped: a negative
  intercept is how the line reaches zero at the balance temperature where
  the house needs no heating (for example 11.5 C).
- shape: per local hour, how much of the daily average the heat pump uses
  then (average 1.0), learned from the same history.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from ..providers.price_forecast import hour_key, linear_fit

HEATING_BASE_C = 18.0
MIN_HOURS_PER_DAY = 18  # a day counts only when most hours were measured
MIN_DAYS = 8  # linear_fit needs eight points; before that a constant level is used


@dataclass(slots=True)
class HeatPumpModel:
    """Fitted level line and hourly shape."""

    a: float = 0.0  # kW at 18 C (usually negative: balance point below 18 C)
    b: float = 0.0  # kW per degree below 18 C
    shape: dict[int, float] = field(default_factory=dict)
    days: int = 0
    r2: float | None = None

    @property
    def balance_temp(self) -> float | None:
        """Daily outdoor temperature above which no heating is expected."""
        return HEATING_BASE_C + self.a / self.b if self.b > 0 else None

    def daily_kw(self, temp: float) -> float:
        """Average power over a day with this mean outdoor temperature."""
        return max(0.0, self.a + self.b * max(0.0, HEATING_BASE_C - temp))

    def power(self, daily_temp: float | None, local_hour: int) -> float:
        """Expected power (kW) in this hour."""
        if daily_temp is None:
            return 0.0
        return self.daily_kw(daily_temp) * self.shape.get(local_hour, 1.0)


def fit_heat_pump(
    power_kw: list[tuple[datetime, float]], temps: dict[datetime, float], tz: tzinfo
) -> HeatPumpModel | None:
    """Fit from hourly mean power (kW) and hourly mean outdoor temperature."""
    by_day: dict[object, list[tuple[int, float, float | None]]] = defaultdict(list)
    for when, kw in power_kw:
        if kw < 0:
            continue
        local = when.astimezone(tz)
        by_day[local.date()].append((local.hour, kw, temps.get(hour_key(when))))
    days = [rows for rows in by_day.values() if len(rows) >= MIN_HOURS_PER_DAY]
    if not days:
        return None

    # Hourly shape: share of the daily average per hour.
    ratios: dict[int, list[float]] = defaultdict(list)
    for rows in days:
        mean = sum(kw for _, kw, _ in rows) / len(rows)
        if mean > 0.05:
            for hour, kw, _ in rows:
                ratios[hour].append(kw / mean)
    shape = {h: sum(v) / len(v) for h, v in ratios.items()}
    if shape:
        norm = sum(shape.values()) / len(shape)
        shape = {h: max(0.2, min(3.0, v / norm)) for h, v in shape.items()}

    # Level line on daily means where the temperature is known.
    points = []
    for rows in days:
        known = [(kw, t) for _, kw, t in rows if t is not None]
        if len(known) >= MIN_HOURS_PER_DAY:
            points.append(
                (
                    max(0.0, HEATING_BASE_C - sum(t for _, t in known) / len(known)),
                    sum(kw for kw, _ in known) / len(known),
                )
            )
    mean_kw = sum(sum(kw for _, kw, _ in rows) / len(rows) for rows in days) / len(days)
    fit = linear_fit(points) if len(points) >= MIN_DAYS else None
    if fit is None or fit[1] <= 0:
        # Not enough spread in temperature yet: constant level.
        return HeatPumpModel(a=mean_kw, b=0.0, shape=shape, days=len(days))
    a, b = fit
    my = sum(y for _, y in points) / len(points)
    total = sum((y - my) ** 2 for _, y in points)
    residual = sum((y - max(0.0, a + b * x)) ** 2 for x, y in points)
    r2 = 1 - residual / total if total > 0 else None
    return HeatPumpModel(a=a, b=b, shape=shape, days=len(points), r2=r2)
