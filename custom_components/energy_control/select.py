"""Mode select."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SET_EV_MODE, SET_MODE, Mode
from .data import EnergyControlConfigEntry, EnergyControlData
from .entity import EnergyControlEntity
from .flexible.ev import EvMode


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyControlConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the mode select."""
    entities: list[SelectEntity] = [ModeSelect(entry.runtime_data)]
    if entry.runtime_data.coordinator.flex.ev_configured:
        entities.append(EvModeSelect(entry.runtime_data))
    async_add_entities(entities)


class ModeSelect(EnergyControlEntity, SelectEntity):
    """Operating mode."""

    _attr_options = [m.value for m in Mode]

    def __init__(self, data: EnergyControlData) -> None:
        """Init."""
        super().__init__(data, "mode")

    @property
    def current_option(self) -> str:
        """Current mode."""
        return self.coordinator.mode.value

    async def async_select_option(self, option: str) -> None:
        """Change mode."""
        await self.coordinator.async_set_setting(SET_MODE, Mode(option).value)


class EvModeSelect(EnergyControlEntity, SelectEntity):
    """EV charging: manual, smart (cheapest before ready-by) or now."""

    _attr_options = [m.value for m in EvMode]

    def __init__(self, data: EnergyControlData) -> None:
        """Init."""
        super().__init__(data, SET_EV_MODE)

    @property
    def current_option(self) -> str:
        """Current EV mode."""
        return self.coordinator.flex.ev_mode.value

    async def async_select_option(self, option: str) -> None:
        """Change EV mode."""
        await self.coordinator.async_set_setting(SET_EV_MODE, EvMode(option).value)
