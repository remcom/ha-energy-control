"""Energy Control: local dynamic ESS optimizer for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, FRONTEND_URL, SIGNAL_ENTRY_LOADED, VERSION
from .control.victron import VictronController
from .coordinator import EnergyControlCoordinator
from .data import EnergyControlConfigEntry, EnergyControlData
from .history import History
from .services import async_setup_services
from .websocket import async_setup_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-wide pieces: frontend card, websocket API, services."""
    if hass.http is not None:
        card = Path(__file__).parent / "frontend" / "energy-control-card.js"
        await hass.http.async_register_static_paths([StaticPathConfig(FRONTEND_URL, str(card), True)])
        if "frontend" in hass.config.components:
            add_extra_js_url(hass, f"{FRONTEND_URL}?v={VERSION}")
    async_setup_websocket(hass)
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = EnergyControlCoordinator(hass, entry)
    await coordinator.async_setup()
    history = History(hass, entry.entry_id)
    await history.async_load()
    controller = VictronController(hass, coordinator, history)
    entry.runtime_data = EnergyControlData(coordinator, controller, history)

    # First plan; failures are tolerated (controller falls back to self consumption).
    await coordinator.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await controller.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    # Let open dashboard subscriptions re-attach after a reload.
    async_dispatcher_send(hass, SIGNAL_ENTRY_LOADED, entry.entry_id, entry.runtime_data)
    return True


async def _async_reload(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> bool:
    """Unload: hand control back to the ESS first."""
    await entry.runtime_data.controller.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
