"""Config flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.energy_control.const import DOMAIN

from .test_init import PRICE, SOC, set_hardware, set_prices


async def test_full_flow(hass: HomeAssistant) -> None:
    """Walk through all steps."""
    set_prices(hass)
    set_hardware(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "prices"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"price_entity": PRICE})
    assert result["step_id"] == "pv"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "battery"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"soc_entity": SOC, "capacity_kwh": 14.3}
    )
    assert result["step_id"] == "load"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "control"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["price_entity"] == PRICE
    assert result["data"]["capacity_kwh"] == 14.3


async def test_price_entity_without_forecast(hass: HomeAssistant) -> None:
    """A sensor without forecast attributes is rejected."""
    hass.states.async_set("sensor.flat", "0.3")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"price_entity": "sensor.flat"})
    assert result["errors"] == {"price_entity": "no_price_forecast"}
