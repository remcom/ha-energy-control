"""Binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .data import EnergyControlConfigEntry
from .entity import EnergyControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyControlConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up binary sensors."""
    data = entry.runtime_data
    async_add_entities([ControlActive(data, "control_active"), Curtailing(data, "curtailing")])


class ControlActive(EnergyControlEntity, BinarySensorEntity):
    """True while Energy Control owns the ESS settings."""

    @property
    def is_on(self) -> bool:
        """State."""
        return self.data.controller.active

    @property
    def extra_state_attributes(self) -> dict:
        """Controller details."""
        c = self.data.controller
        d = c.desired
        return {
            "status": c.status,
            "last_error": c.last_error,
            "setpoint_w": d.setpoint if d else None,
            "charge_limit_w": d.charge_limit if d else None,
            "discharge_limit_w": d.discharge_limit if d else None,
            "feed_in_limit_w": d.feed_in_limit if d else None,
        }


class Curtailing(EnergyControlEntity, BinarySensorEntity):
    """True while PV is curtailed because of negative prices."""

    @property
    def is_on(self) -> bool:
        """State."""
        return self.data.controller.curtailing
