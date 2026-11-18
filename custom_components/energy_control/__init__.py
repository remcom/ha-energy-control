"""Energy Control: local dynamic ESS optimizer for Home Assistant."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import DEFAULTS, DOMAIN, FRONTEND_URL, SIGNAL_ENTRY_LOADED
from .control.victron import VictronController
from .coordinator import EnergyControlCoordinator
from .data import EnergyControlConfigEntry, EnergyControlData
from .history import History
from .services import async_setup_services
from .websocket import async_setup_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-wide pieces: frontend card, websocket API, services."""
    if hass.http is not None:
        card = Path(__file__).parent / "frontend" / "energy-control-card.js"
        await hass.http.async_register_static_paths([StaticPathConfig(FRONTEND_URL, str(card), True)])
        if "frontend" in hass.config.components:
            # Cache-bust on content: a changed card reaches browsers without a version bump.
            digest = await hass.async_add_executor_job(_file_digest, card)
            add_extra_js_url(hass, f"{FRONTEND_URL}?v={digest}")
    async_setup_websocket(hass)
    async_setup_services(hass)
    return True


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


async def async_setup_entry(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = EnergyControlCoordinator(hass, entry)
    history = History(hass, entry.entry_id)
    controller = VictronController(hass, coordinator, history)
    await controller.async_load()
    try:
        await coordinator.async_setup()
        await history.async_load()
        coordinator.history = history
        entry.runtime_data = EnergyControlData(coordinator, controller, history)

        # First plan; failures are tolerated (controller falls back to self consumption).
        await coordinator.async_refresh()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await controller.async_start()
    except Exception:
        # Never leave the ESS in a forced state we no longer manage.
        await controller.async_stop()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    # Let open dashboard subscriptions re-attach after a reload.
    async_dispatcher_send(hass, SIGNAL_ENTRY_LOADED, entry.entry_id, entry.runtime_data)
    return True


async def _async_reload(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> None:
    # Options changed: hand control over to the reloaded entry instead of a
    # restore + re-takeover (see VictronController.reload_config).
    entry.runtime_data.controller.reload_config = {**DEFAULTS, **(entry.options or entry.data)}
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> bool:
    """Unload: hand control back to the ESS first."""
    controller = entry.runtime_data.controller
    await controller.async_stop()
    # Never leave the heat pump blocked by a plan nobody executes any more.
    await entry.runtime_data.coordinator.flex.async_release()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded and controller.reload_config is not None:
        # The reload stops here: nobody would own the hardware.
        controller.reload_config = None
        await controller.async_release()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> None:
    """Delete the entry's stores (control was already released on unload)."""
    for suffix in ("settings", "history", "originals", "pending_restores", "price_archive", "flexible"):
        await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.{suffix}").async_remove()
