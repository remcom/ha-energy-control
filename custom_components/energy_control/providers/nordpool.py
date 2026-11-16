"""Native core Nord Pool forecasts via the response action (EUR/MWh)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .prices import PricePoint, parse_price_attributes

_LOGGER = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("Europe/Stockholm")


class NordPoolForecast:
    """Cache published days; retry missing tomorrow around publication time."""

    def __init__(self, hass: HomeAssistant, entry_id: str, area: str) -> None:
        """Configure an explicit market area and request EUR regardless of sensor currency."""
        self.hass = hass
        self.entry_id = entry_id
        self.area = area.upper()
        self._cache: dict[date, list[PricePoint]] = {}
        self._retry: dict[date, datetime] = {}

    async def async_prices(self, now: datetime) -> list[PricePoint]:
        """Read today and tomorrow, retaining published data during temporary failures."""
        today = now.astimezone(MARKET_TZ).date()
        self._cache = {day: points for day, points in self._cache.items() if day >= today}
        self._retry = {day: retry for day, retry in self._retry.items() if day >= today}
        result = []
        for day in (today, today + timedelta(days=1)):
            if day not in self._cache and now >= self._retry.get(day, now):
                try:
                    response = await self.hass.services.async_call(
                        "nordpool",
                        "get_prices_for_date",
                        {
                            "config_entry": self.entry_id,
                            "date": day.isoformat(),
                            "areas": [self.area],
                            "currency": "EUR",
                        },
                        blocking=True,
                        return_response=True,
                    )
                    rows = response.get(self.area, []) if response else []
                    points = parse_price_attributes({"prices": rows}, "EUR/MWh")
                    if points:
                        # Core action prices are raw spot; preserve that explicitly.
                        self._cache[day] = [PricePoint(p.start, p.minutes, p.price, p.price) for p in points]
                except (HomeAssistantError, ValueError) as err:
                    _LOGGER.debug("Nord Pool %s %s not available: %s", self.area, day, err)
                publication = datetime.combine(today, datetime.min.time(), MARKET_TZ).replace(hour=13)
                if day > today and now < publication:
                    self._retry[day] = publication
                else:
                    self._retry[day] = now + timedelta(minutes=15 if day > today else 5)
            result.extend(self._cache.get(day, []))
        return sorted({p.start: p for p in result}.values(), key=lambda p: p.start)
