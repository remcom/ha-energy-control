"""Shared fixtures."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.energy_control.const import (
    CONF_CAPACITY_KWH,
    CONF_CHARGE_LIMIT_ENTITY,
    CONF_DESS_MODE_ENTITY,
    CONF_DISCHARGE_LIMIT_ENTITY,
    CONF_FEED_IN_LIMIT_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PV_LIMIT_ENTITY,
    CONF_SETPOINT_ENTITY,
    CONF_SOC_ENTITY,
    DOMAIN,
)

from .common import CHARGE, DESS, DISCHARGE, FEED_IN, PRICE, PV_LIMIT, SETPOINT, SOC, set_hardware, set_prices


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A configured entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Energy Control",
        data={
            CONF_PRICE_ENTITY: PRICE,
            CONF_SOC_ENTITY: SOC,
            CONF_CAPACITY_KWH: 10.0,
            CONF_SETPOINT_ENTITY: SETPOINT,
            CONF_CHARGE_LIMIT_ENTITY: CHARGE,
            CONF_DISCHARGE_LIMIT_ENTITY: DISCHARGE,
            CONF_FEED_IN_LIMIT_ENTITY: FEED_IN,
            CONF_DESS_MODE_ENTITY: DESS,
            CONF_PV_LIMIT_ENTITY: PV_LIMIT,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup(hass: HomeAssistant, entry: MockConfigEntry, freezer):
    """Set up the integration with mocked hardware services."""
    # Pin the clock just after a quarter boundary: late in a quarter the (nearly elapsed)
    # current slot is too short to be worth charging and the plan starts in the next one.
    freezer.move_to("2026-11-15T08:01:00+00:00")
    await async_setup_component(hass, "http", {})
    set_prices(hass)
    set_hardware(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Mock after setup: loading our own number/select platforms registers the real services.
    number_calls = async_mock_service(hass, "number", "set_value")
    select_calls = []

    async def select(call):
        select_calls.append(call)
        state = hass.states.get(call.data["entity_id"])
        hass.states.async_set(call.data["entity_id"], call.data["option"], state.attributes)

    hass.services.async_register("select", "select_option", select)
    return number_calls, select_calls
