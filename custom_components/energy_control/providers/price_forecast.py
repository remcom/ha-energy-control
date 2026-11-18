"""Price estimates beyond the published day-ahead prices. Pure Python.

Day-ahead prices for tomorrow are published around 13:00. Before that the
plan would end at midnight and could not see tomorrow's morning peak. The
estimate fills that gap so the value of energy left in the battery is
realistic. It never drives what happens now: by the time an estimated
quarter arrives, the real price has normally been published and replaces it
(and the controller falls back to self consumption if it has not).

Model (all-in import price, EUR/kWh), per quarter t:

    price(t) = profile(t)                      recency-weighted typical price for
                                               this quarter of a weekday/weekend
             + b0                              intercept
             + b1 * level                      persistence: how far the last 24 h
                                               of published prices were above/below
                                               their profile (gas price, weather regime)
             + b2 * solar_anomaly(t)           more sun than usual for this hour:
                                               lower midday prices
             + b3 * wind_anomaly(t)            more wind than usual: lower prices

The coefficients are fitted with ridge regression towards physically
sensible priors, so the model behaves well from day one and learns the local
market from the archive as it grows. An external forecast (for example
energyforecast.de via EPEX Spot) can replace the built-in estimate; it is
calibrated against the published prices first.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from math import isfinite

from .prices import PricePoint, price_at

QUARTER = timedelta(minutes=15)
HALF_LIFE_DAYS = 7.0  # profile weight halves every week: follows seasons and fuel prices
PRIORS = (0.0, 0.6, -0.06, -0.04)  # intercept, level persistence, solar, wind (EUR/kWh)
RIDGE = 192.0  # prior strength in quarters (two days of data)
WIND_SCALE_MS = 10.0  # wind index 1.0 = 10 m/s


@dataclass(frozen=True, slots=True)
class Observation:
    """A published price (archive row)."""

    start: datetime
    price: float  # all-in import price, EUR/kWh
    spot: float | None = None


@dataclass(frozen=True, slots=True)
class Weather:
    """Weather drivers for one hour."""

    solar_kw: float | None = None  # own PV forecast: proxy for national solar
    wind_ms: float | None = None  # local wind speed: proxy for national wind


def hour_key(when: datetime) -> datetime:
    """UTC hour bucket used for weather features."""
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class PriceEstimator:
    """Fitted price model; build once per plan."""

    def __init__(
        self,
        history: Iterable[Observation],
        weather: dict[datetime, Weather],
        tz: tzinfo,
        now: datetime,
    ) -> None:
        """Fit the profile and the weather/persistence coefficients."""
        self.tz = tz
        self.now = now
        self.weather = weather
        rows = sorted((o for o in history if isfinite(o.price) and o.start < now), key=lambda o: o.start)
        self.rows = rows
        self._fit_profile(rows)
        self._fit_weather_norms()
        self.coef = self._fit(rows)
        prices = sorted(o.price for o in rows)
        self.bounds = (
            (prices[int(0.01 * (len(prices) - 1))], prices[int(0.99 * (len(prices) - 1))]) if prices else (-1.0, 2.0)
        )

    @property
    def usable(self) -> bool:
        """At least three days (72 h of quarters) of history: enough for a daily profile."""
        return len(self.rows) >= 3 * 96

    # --- profile -----------------------------------------------------------
    def _local(self, when: datetime) -> datetime:
        return when.astimezone(self.tz)

    def _key(self, when: datetime) -> tuple[bool, int]:
        local = self._local(when)
        return local.weekday() >= 5, local.hour * 4 + local.minute // 15

    def _fit_profile(self, rows: list[Observation]) -> None:
        sums: dict[tuple[bool, int], float] = defaultdict(float)
        weights: dict[tuple[bool, int], float] = defaultdict(float)
        hour_sums: dict[int, float] = defaultdict(float)
        hour_weights: dict[int, float] = defaultdict(float)
        total = weight_total = 0.0
        for o in rows:
            age_days = max(0.0, (self.now - o.start).total_seconds() / 86400)
            w = 0.5 ** (age_days / HALF_LIFE_DAYS)
            key = self._key(o.start)
            sums[key] += w * o.price
            weights[key] += w
            hour_sums[key[1] // 4] += w * o.price
            hour_weights[key[1] // 4] += w
            total += w * o.price
            weight_total += w
        self._profile = {k: sums[k] / weights[k] for k in sums}
        self._hour_profile = {h: hour_sums[h] / hour_weights[h] for h in hour_sums}
        self._mean = total / weight_total if weight_total else 0.25

    def profile(self, when: datetime) -> float:
        """Typical price for this quarter (weekday/weekend aware)."""
        key = self._key(when)
        if key in self._profile:
            return self._profile[key]
        other = (not key[0], key[1])
        if other in self._profile:
            return self._profile[other]
        return self._hour_profile.get(key[1] // 4, self._mean)

    # --- features ----------------------------------------------------------
    def _fit_weather_norms(self) -> None:
        solar = [w.solar_kw for w in self.weather.values() if w.solar_kw is not None]
        self._solar_peak = max(0.1, sorted(solar)[int(0.95 * (len(solar) - 1))]) if solar else 1.0
        by_hour: dict[int, list[float]] = defaultdict(list)
        for when, w in self.weather.items():
            if w.solar_kw is not None:
                by_hour[self._local(when).hour].append(w.solar_kw / self._solar_peak)
        self._solar_mean = {h: sum(v) / len(v) for h, v in by_hour.items()}
        wind = [w.wind_ms for w in self.weather.values() if w.wind_ms is not None]
        self._wind_mean = sum(wind) / len(wind) / WIND_SCALE_MS if wind else 0.0

    def _features(self, when: datetime, level: float) -> tuple[float, float, float, float]:
        w = self.weather.get(hour_key(when), Weather())
        hour = self._local(when).hour
        solar = 0.0
        if w.solar_kw is not None:
            solar = w.solar_kw / self._solar_peak - self._solar_mean.get(hour, 0.0)
        wind = 0.0
        if w.wind_ms is not None:
            wind = w.wind_ms / WIND_SCALE_MS - self._wind_mean
        return 1.0, level, solar, wind

    def level_before(self, when: datetime, points: Iterable[Observation] | None = None) -> float:
        """Mean deviation from the profile over the 24 h before `when`."""
        source = self.rows if points is None else points
        residuals = [o.price - self.profile(o.start) for o in source if when - timedelta(hours=24) <= o.start < when]
        return sum(residuals) / len(residuals) if len(residuals) >= 16 else 0.0

    # --- fit ---------------------------------------------------------------
    def _fit(self, rows: list[Observation]) -> tuple[float, ...]:
        """Ridge regression of the residual towards PRIORS.

        The prior weighs as much as RIDGE quarters of data with the feature's
        own spread, so it fades at the same pace for every feature.
        """
        n = len(PRIORS)
        levels: dict[object, float] = {}
        data = []
        for o in rows:
            day = self._local(o.start).date()
            if day not in levels:
                midnight = datetime.combine(day, datetime.min.time(), self.tz)
                levels[day] = self.level_before(midnight)
            data.append((self._features(o.start, levels[day]), o.price - self.profile(o.start)))
        strength = [RIDGE]
        for i in range(1, n):
            values = [x[i] for x, _ in data]
            mean = sum(values) / len(values) if values else 0.0
            variance = sum((v - mean) ** 2 for v in values) / len(values) if values else 0.0
            strength.append(RIDGE * max(variance, 1e-4))
        xtx = [[strength[i] if i == j else 0.0 for j in range(n)] for i in range(n)]
        xty = [strength[i] * PRIORS[i] for i in range(n)]
        for x, y in data:
            for i in range(n):
                xty[i] += x[i] * y
                for j in range(n):
                    xtx[i][j] += x[i] * x[j]
        coef = _solve(xtx, xty)
        return coef if coef is not None else PRIORS

    # --- estimate ----------------------------------------------------------
    def estimate(self, start: datetime, end: datetime, published: list[Observation]) -> list[PricePoint]:
        """Quarter estimates in [start, end), after the published prices."""
        level = self.level_before(start, [*self.rows, *published])
        low, high = self.bounds
        margin = 0.1 * (high - low)
        points = []
        when = start
        while when < end:
            x = self._features(when, level)
            value = self.profile(when) + sum(c * v for c, v in zip(self.coef, x, strict=True))
            value = max(low - margin, min(high + margin, value))
            points.append(PricePoint(when, 15, value, None, estimated=True))
            when += QUARTER
        return points


def _solve(a: list[list[float]], b: list[float]) -> tuple[float, ...] | None:
    """Gaussian elimination with partial pivoting (small systems)."""
    n = len(b)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                factor = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    return tuple(m[i][n] / m[i][i] for i in range(n))


def linear_fit(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least squares y = a + b x; None when x does not vary."""
    if len(pairs) < 8:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    if sxx < 1e-9:
        return None
    b = sum((x - mx) * (y - my) for x, y in pairs) / sxx
    return my - b * mx, b


def spot_relation(published: list[PricePoint]) -> tuple[float, float] | None:
    """all-in = a + b * spot, from published points that carry both."""
    fit = linear_fit([(p.spot, p.price) for p in published if p.spot is not None])
    return fit if fit and fit[1] > 0.1 else None


def calibrate_external(external: list[PricePoint], published: list[PricePoint]) -> Callable[[float], float]:
    """Map an external forecast value to the all-in import price.

    With enough overlap the mapping is fitted directly (works for spot and
    all-in forecasts alike); otherwise the forecast is treated as a spot
    price and converted with the supplier's own spot relation.
    """
    pairs = [
        (point.price, known.price)
        for point in external
        if (known := price_at(published, point.start)) is not None and not known.estimated
    ]
    fit = linear_fit(pairs)
    if fit is not None and fit[1] > 0.1:
        return lambda v: fit[0] + fit[1] * v
    relation = spot_relation(published)
    if relation is not None:
        return lambda v: relation[0] + relation[1] * v
    return lambda v: v


def with_spot(points: list[PricePoint], relation: tuple[float, float] | None) -> list[PricePoint]:
    """Derive the spot part of estimates from the supplier's spot relation."""
    if relation is None:
        return points
    a, b = relation
    return [PricePoint(p.start, p.minutes, p.price, (p.price - a) / b, estimated=True) for p in points]
