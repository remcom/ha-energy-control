"""Strategy switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SET_BATTERY_TO_GRID, SET_CURTAIL_NEGATIVE, SET_GRID_CHARGE, SET_PRICE_ESTIMATES
from .data import EnergyControlConfigEntry, EnergyControlData
from .entity import EnergyControlEntity

SWITCHES = (SET_GRID_CHARGE, SET_BATTERY_TO_GRID, SET_CURTAIL_NEGATIVE, SET_PRICE_ESTIMATES)


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyControlConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up strategy switches."""
    async_add_entities(StrategySwitch(entry.runtime_data, key) for key in SWITCHES)


class StrategySwitch(EnergyControlEntity, SwitchEntity):
    """Enable/disable a strategy."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, data: EnergyControlData, key: str) -> None:
        """Init."""
        super().__init__(data, key)
        self._key = key

    @property
    def is_on(self) -> bool:
        """State."""
        return bool(self.coordinator.settings[self._key])

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        await self.coordinator.async_set_setting(self._key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        await self.coordinator.async_set_setting(self._key, False)
