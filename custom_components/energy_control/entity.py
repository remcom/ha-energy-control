"""Base entity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .coordinator import EnergyControlCoordinator

if TYPE_CHECKING:
    from .data import EnergyControlData


class EnergyControlEntity(CoordinatorEntity[EnergyControlCoordinator]):
    """Common base: one service device per config entry."""

    _attr_has_entity_name = True

    def __init__(self, data: EnergyControlData, key: str) -> None:
        """Init."""
        super().__init__(data.coordinator)
        self.data = data
        entry_id = data.coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="Energy Control",
            model="Battery optimizer",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Also follow controller state."""
        await super().async_added_to_hass()
        self.async_on_remove(self.data.controller.async_add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        """Settings entities stay available when planning fails."""
        return True
