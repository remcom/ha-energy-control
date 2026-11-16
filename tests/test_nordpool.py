"""Native Nord Pool response conversion, caching, retry and midnight behavior."""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.energy_control.providers.nordpool import NordPoolForecast


@pytest.mark.parametrize("minutes", [15, 60])
async def test_nordpool_converts_eur_mwh_and_caches(hass, freezer, minutes):
    freezer.move_to("2026-11-15T10:00:00+00:00")
    requests = []

    async def prices(call):
        requests.append(call.data)
        start = datetime.fromisoformat(call.data["date"]).replace(tzinfo=UTC)
        return {
            "NL": [{"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat(), "price": -50}]
        }

    hass.services.async_register("nordpool", "get_prices_for_date", prices, supports_response=SupportsResponse.ONLY)
    provider = NordPoolForecast(hass, "nordpool-entry", "NL")
    points = await provider.async_prices(dt_util.utcnow())
    assert len(points) == 2
    assert all(p.minutes == minutes and p.price == -0.05 and p.spot == -0.05 for p in points)
    assert all(r["currency"] == "EUR" and r["areas"] == ["NL"] for r in requests)
    await provider.async_prices(dt_util.utcnow())
    assert len(requests) == 2


async def test_missing_tomorrow_waits_for_publication_then_retries(hass, freezer):
    freezer.move_to("2026-11-15T10:00:00+00:00")
    requests = []
    published = False

    async def prices(call):
        requests.append(call.data["date"])
        if call.data["date"] == "2026-11-16" and not published:
            raise HomeAssistantError("not published")
        start = datetime.fromisoformat(call.data["date"]).replace(tzinfo=UTC)
        return {"NL": [{"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "price": 100}]}

    hass.services.async_register("nordpool", "get_prices_for_date", prices, supports_response=SupportsResponse.ONLY)
    provider = NordPoolForecast(hass, "entry", "NL")
    assert len(await provider.async_prices(dt_util.utcnow())) == 1
    freezer.tick(timedelta(minutes=30))
    await provider.async_prices(dt_util.utcnow())
    assert len(requests) == 2
    freezer.move_to("2026-11-15T12:00:00+00:00")  # 13:00 CET
    await provider.async_prices(dt_util.utcnow())
    assert len(requests) == 3
    freezer.tick(timedelta(minutes=10))
    published = True
    assert len(await provider.async_prices(dt_util.utcnow())) == 1
    freezer.tick(timedelta(minutes=5))
    assert len(await provider.async_prices(dt_util.utcnow())) == 2
    # Midnight: retain the cached tomorrow as today; only fetch the new tomorrow.
    freezer.move_to("2026-11-16T00:00:00+00:00")
    assert len(await provider.async_prices(dt_util.utcnow())) == 2
    assert requests.count("2026-11-16") == 3
    assert requests[-1] == "2026-11-17"


async def test_native_provider_plans_without_forecast_sensor(hass, entry, freezer):
    from homeassistant.setup import async_setup_component

    from .common import set_hardware

    freezer.move_to("2026-11-15T08:01:00+00:00")
    hass.config_entries.async_update_entry(
        entry,
        data={
            **{key: value for key, value in entry.data.items() if key != "price_entity"},
            "nordpool_entry": "native-entry",
            "nordpool_area": "NL",
            "energy_tax": 0.1,
            "vat": 21,
        },
    )

    async def prices(call):
        start = datetime.fromisoformat(call.data["date"]).replace(tzinfo=UTC)
        return {
            "NL": [
                {
                    "start": (start + timedelta(hours=i)).isoformat(),
                    "end": (start + timedelta(hours=i + 1)).isoformat(),
                    "price": 100,
                }
                for i in range(24)
            ]
        }

    hass.services.async_register("nordpool", "get_prices_for_date", prices, supports_response=SupportsResponse.ONLY)
    await async_setup_component(hass, "http", {})
    set_hardware(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    c = entry.runtime_data.coordinator
    assert c.last_update_success
    assert c.data.slots
    assert c.data.slots[0].import_price == pytest.approx((0.1 + 0.1) * 1.21)
    assert c.data.export_prices_complete
    assert c.data.slots[0].export_price == pytest.approx(c.data.slots[0].import_price)
