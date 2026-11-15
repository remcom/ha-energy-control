"""Services."""

from __future__ import annotations

from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import Override
from .data import EnergyControlData
from .optimizer import Action

ATTR_ENTRY_ID = "config_entry_id"

BASE = {vol.Optional(ATTR_ENTRY_ID): cv.string}
OVERRIDE_SCHEMA = vol.Schema(
    {
        **BASE,
        vol.Required("action"): vol.In([a.value for a in Action]),
        vol.Required("duration"): cv.positive_time_period,
        vol.Optional("power"): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)


def _entries(hass: HomeAssistant, call: ServiceCall) -> list[EnergyControlData]:
    wanted = call.data.get(ATTR_ENTRY_ID)
    found = [
        e.runtime_data
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED and (wanted is None or e.entry_id == wanted)
    ]
    if not found:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entry")
    return found


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register services."""

    async def replan(call: ServiceCall) -> None:
        for data in _entries(hass, call):
            data.coordinator.load_profile = None  # also refresh the load profile
            await data.coordinator.async_refresh()

    async def override(call: ServiceCall) -> None:
        duration: timedelta = call.data["duration"]
        for data in _entries(hass, call):
            data.coordinator.override = Override(
                action=Action(call.data["action"]),
                until=dt_util.utcnow() + duration,
                power_w=call.data.get("power"),
            )
            await data.controller.async_apply()

    async def clear_override(call: ServiceCall) -> None:
        for data in _entries(hass, call):
            data.coordinator.override = None
            await data.controller.async_apply()

    hass.services.async_register(DOMAIN, "replan", replan, schema=vol.Schema(BASE))
    hass.services.async_register(DOMAIN, "override", override, schema=OVERRIDE_SCHEMA)
    hass.services.async_register(DOMAIN, "clear_override", clear_override, schema=vol.Schema(BASE))
