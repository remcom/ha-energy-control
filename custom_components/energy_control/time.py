"""EV ready-by time."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SET_EV_READY_BY
from .data import EnergyControlConfigEntry, EnergyControlData
from .entity import EnergyControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyControlConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up the ready-by time when an EV charger is configured."""
    if entry.runtime_data.coordinator.flex.ev_configured:
        async_add_entities([EvReadyBy(entry.runtime_data)])


class EvReadyBy(EnergyControlEntity, TimeEntity):
    """Time the car must be charged by."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, data: EnergyControlData) -> None:
        """Init."""
        super().__init__(data, SET_EV_READY_BY)

    @property
    def native_value(self) -> time:
        """Ready-by time."""
        return self.coordinator.flex.ev_ready_by

    async def async_set_value(self, value: time) -> None:
        """Set ready-by time."""
        await self.coordinator.async_set_setting(SET_EV_READY_BY, value.replace(second=0, microsecond=0).isoformat())
