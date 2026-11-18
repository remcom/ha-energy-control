"""Services."""

from __future__ import annotations

from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import CONTROL_MODES, DOMAIN
from .coordinator import Override
from .data import EnergyControlData
from .flexible.window import cheapest_window
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


WINDOW_SCHEMA = vol.Schema(
    {
        **BASE,
        vol.Required("duration"): cv.positive_time_period,
        vol.Optional("power", default=1000): vol.All(vol.Coerce(float), vol.Range(min=1)),
        vol.Optional("earliest"): cv.datetime,
        vol.Optional("latest_end"): cv.datetime,
        vol.Optional("contiguous", default=True): cv.boolean,
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
        entries = _entries(hass, call)
        for data in entries:
            if data.coordinator.mode not in CONTROL_MODES or not data.controller.can_control:
                # The controller ignores overrides outside control modes: say so.
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="override_needs_control",
                    translation_placeholders={"mode": data.coordinator.mode.value},
                )
        for data in entries:
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

    async def find_window(call: ServiceCall) -> ServiceResponse:
        data = _entries(hass, call)[0]
        plan = data.coordinator.data
        if plan is None or not plan.slots:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_plan")
        tz = dt_util.get_default_time_zone()

        def aware(value):
            return value if value is None or value.tzinfo else value.replace(tzinfo=tz)

        window = cheapest_window(
            plan.slots,
            call.data["duration"],
            call.data["power"] / 1000,
            earliest=aware(call.data.get("earliest")) or dt_util.utcnow(),
            latest_end=aware(call.data.get("latest_end")),
            contiguous=call.data["contiguous"],
        )
        if window is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_window")
        return {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "average_price": round(window.average_price, 5),
            "cost": round(window.cost, 4),
            "energy_kwh": round(window.energy_kwh, 3),
            "price_estimated": window.price_estimated,
            "runs": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in window.slots]
            or [{"start": window.start.isoformat(), "end": window.end.isoformat()}],
        }

    hass.services.async_register(
        DOMAIN, "cheapest_window", find_window, schema=WINDOW_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(DOMAIN, "replan", replan, schema=vol.Schema(BASE))
    hass.services.async_register(DOMAIN, "override", override, schema=OVERRIDE_SCHEMA)
    hass.services.async_register(DOMAIN, "clear_override", clear_override, schema=vol.Schema(BASE))
