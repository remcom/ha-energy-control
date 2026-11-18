"""Tunable parameters."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_EV_SOC_ENTITY,
    DOMAIN,
    SET_ARBITRAGE_MARGIN,
    SET_CYCLE_COST,
    SET_EV_ENERGY,
    SET_EV_TARGET_SOC,
    SET_MAX_SOC,
    SET_MIN_SOC,
)
from .data import EnergyControlConfigEntry, EnergyControlData
from .entity import EnergyControlEntity


@dataclass(frozen=True, kw_only=True)
class SettingNumberDescription(NumberEntityDescription):
    """Number bound to a runtime setting."""


NUMBERS = (
    SettingNumberDescription(
        key=SET_MIN_SOC,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
    ),
    SettingNumberDescription(
        key=SET_MAX_SOC,
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
    ),
    SettingNumberDescription(
        key=SET_CYCLE_COST,
        native_min_value=0,
        native_max_value=50,
        native_step=0.1,
        native_unit_of_measurement="ct/kWh",
    ),
    SettingNumberDescription(
        key=SET_ARBITRAGE_MARGIN,
        native_min_value=0,
        native_max_value=50,
        native_step=0.1,
        native_unit_of_measurement="ct/kWh",
    ),
)


EV_ENERGY = SettingNumberDescription(
    key=SET_EV_ENERGY,
    native_min_value=0,
    native_max_value=200,
    native_step=0.5,
    native_unit_of_measurement="kWh",
)
EV_TARGET = SettingNumberDescription(
    key=SET_EV_TARGET_SOC,
    native_min_value=10,
    native_max_value=100,
    native_step=1,
    native_unit_of_measurement=PERCENTAGE,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyControlConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up numbers."""
    data = entry.runtime_data
    descriptions = list(NUMBERS)
    if data.coordinator.flex.ev_configured:
        descriptions.append(EV_TARGET if data.coordinator.config.get(CONF_EV_SOC_ENTITY) else EV_ENERGY)
    async_add_entities(SettingNumber(data, d) for d in descriptions)


class SettingNumber(EnergyControlEntity, NumberEntity):
    """A runtime setting."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    entity_description: SettingNumberDescription

    def __init__(self, data: EnergyControlData, description: SettingNumberDescription) -> None:
        """Init."""
        super().__init__(data, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        """Value."""
        return float(self.coordinator.settings[self.entity_description.key])

    async def async_set_native_value(self, value: float) -> None:
        """Set."""
        key = self.entity_description.key
        settings = self.coordinator.settings
        if (key == SET_MIN_SOC and value > float(settings[SET_MAX_SOC])) or (
            key == SET_MAX_SOC and value < float(settings[SET_MIN_SOC])
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="min_soc_above_max",
                translation_placeholders={
                    "min_soc": str(value if key == SET_MIN_SOC else settings[SET_MIN_SOC]),
                    "max_soc": str(value if key == SET_MAX_SOC else settings[SET_MAX_SOC]),
                },
            )
        await self.coordinator.async_set_setting(self.entity_description.key, value)
