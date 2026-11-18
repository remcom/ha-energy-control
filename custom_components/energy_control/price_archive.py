"""Archive of published prices and weather drivers for the price estimator."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .providers.price_forecast import QUARTER, Observation, Weather, hour_key
from .providers.prices import PricePoint, unit_scale
from .providers.solar import EnergyInterval, energy_between
from .providers.util import parse_dt, to_float

_LOGGER = logging.getLogger(__name__)

ARCHIVE_DAYS = 35
BOOTSTRAP_DAYS = 14
WEATHER_REFRESH = timedelta(minutes=60)
WIND_TO_MS = {"m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.514444, "ft/s": 0.3048}


class PriceArchive:
    """Published quarter prices and hourly weather drivers, persisted."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Init."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.price_archive")
        self.prices: dict[str, tuple[float, float | None]] = {}
        self.weather: dict[str, tuple[float | None, float | None]] = {}
        self._wind: dict[datetime, float] = {}
        self.temperature: dict[datetime, float] = {}  # hourly outdoor forecast (°C)
        self._wind_at: datetime | None = None
        self._bootstrapped = False

    async def async_load(self) -> None:
        """Load from disk."""
        data = await self._store.async_load() or {}
        self.prices = {k: (v[0], v[1]) for k, v in data.get("prices", {}).items()}
        self.weather = {k: (v[0], v[1]) for k, v in data.get("weather", {}).items()}

    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "prices": {k: list(v) for k, v in self.prices.items()},
                "weather": {k: list(v) for k, v in self.weather.items()},
            },
            60,
        )

    # --- prices ------------------------------------------------------------
    def add_published(self, points: list[PricePoint], now: datetime) -> None:
        """Archive published prices per quarter (hourly prices are split)."""
        changed = False
        for point in points:
            if point.estimated:
                continue
            when = point.start
            while when < point.end:
                key = when.astimezone(dt_util.UTC).isoformat()
                value = (round(point.price, 6), None if point.spot is None else round(point.spot, 6))
                if self.prices.get(key) != value:
                    self.prices[key] = value
                    changed = True
                when += QUARTER
        if changed:
            self._prune(now)
            self._save()

    def observations(self) -> list[Observation]:
        """Archived prices."""
        rows = []
        for key, (price, spot) in self.prices.items():
            start = dt_util.parse_datetime(key)
            if start is not None:
                rows.append(Observation(start, price, spot))
        return rows

    def days(self) -> int:
        """Number of distinct days with archived prices."""
        return len({key[:10] for key in self.prices})

    async def async_bootstrap(self, entity_id: str, now: datetime) -> None:
        """Seed an empty archive from the price sensor's recorder history.

        The state of a price sensor is the current all-in price, so its
        history is exactly the training data. Runs once per start.
        """
        if self._bootstrapped or self.days() >= 3 or "recorder" not in self.hass.config.components:
            return
        self._bootstrapped = True
        from homeassistant.components.recorder import get_instance, history

        start = now - timedelta(days=BOOTSTRAP_DAYS)
        try:
            states = await get_instance(self.hass).async_add_executor_job(
                history.state_changes_during_period, self.hass, start, now, entity_id
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("No price history for %s: %s", entity_id, err)
            return
        rows = states.get(entity_id, [])
        added = 0
        for state, following in zip(rows, [*rows[1:], None], strict=True):
            value = to_float(state.state)
            if value is None:
                continue
            try:
                scale = unit_scale(state.attributes.get("unit_of_measurement"))
            except ValueError:
                return
            begin = state.last_changed
            end = following.last_changed if following is not None else now
            # A state holds until the next change; archive whole quarters only.
            when = begin.replace(minute=begin.minute - begin.minute % 15, second=0, microsecond=0)
            if when < begin:
                when += QUARTER
            while when + QUARTER <= end and when < now:
                key = when.astimezone(dt_util.UTC).isoformat()
                if key not in self.prices:
                    self.prices[key] = (round(value * scale, 6), None)
                    added += 1
                when += QUARTER
        if added:
            _LOGGER.info("Seeded the price archive with %d quarters from %s", added, entity_id)
            self._prune(now)
            self._save()

    # --- weather -----------------------------------------------------------
    async def async_update_weather(self, weather_entity: str | None, pv: list[EnergyInterval], now: datetime) -> None:
        """Record solar (own PV forecast) and wind for the coming hours.

        Hours keep the last forecast made before they passed: the same kind
        of information the estimator has when it predicts.
        """
        if weather_entity and (self._wind_at is None or now - self._wind_at >= WEATHER_REFRESH):
            self._wind_at = now
            self._wind, self.temperature = await self._async_forecast(weather_entity)
        current = hour_key(now)
        changed = False
        for hours_ahead in range(0, 72):
            hour = current + timedelta(hours=hours_ahead)
            solar = energy_between(pv, hour, hour + timedelta(hours=1)) if pv else None
            wind = self._wind.get(hour)
            if solar is None and wind is None:
                continue
            key = hour.isoformat()
            old = self.weather.get(key, (None, None))
            value = (
                round(solar, 3) if solar is not None else old[0],
                round(wind, 2) if wind is not None else old[1],
            )
            if value != old:
                self.weather[key] = value
                changed = True
        if changed:
            self._save()

    async def _async_forecast(self, entity_id: str) -> tuple[dict[datetime, float], dict[datetime, float]]:
        """Hourly wind (m/s) and temperature (°C) forecasts."""
        state = self.hass.states.get(entity_id)
        factor = WIND_TO_MS.get(str(state.attributes.get("wind_speed_unit")) if state else "", 1 / 3.6)
        fahrenheit = state is not None and state.attributes.get("temperature_unit") == "°F"
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("No hourly weather forecast from %s: %s", entity_id, err)
            return self._wind, self.temperature
        rows = ((response or {}).get(entity_id) or {}).get("forecast") or []
        wind: dict[datetime, float] = {}
        temperature: dict[datetime, float] = {}
        for row in rows:
            when = parse_dt(row.get("datetime"))
            if when is None:
                continue
            speed = to_float(row.get("wind_speed"))
            if speed is not None and speed >= 0:
                wind[hour_key(when)] = speed * factor
            temp = to_float(row.get("temperature"))
            if temp is not None:
                temperature[hour_key(when)] = (temp - 32) / 1.8 if fahrenheit else temp
        return wind or self._wind, temperature or self.temperature

    def weather_map(self) -> dict[datetime, Weather]:
        """Archived weather drivers per UTC hour."""
        result = {}
        for key, (solar, wind) in self.weather.items():
            when = dt_util.parse_datetime(key)
            if when is not None:
                result[when] = Weather(solar, wind)
        return result

    def _prune(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=ARCHIVE_DAYS)).astimezone(dt_util.UTC).isoformat()
        self.prices = {k: v for k, v in self.prices.items() if k >= cutoff}
        self.weather = {k: v for k, v in self.weather.items() if k >= cutoff}
