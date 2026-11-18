"""Config and options flow for Energy Control."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector as sel

from .const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFF,
    CONF_CHARGE_LIMIT_ENTITY,
    CONF_DESS_MODE_ENTITY,
    CONF_DISCHARGE_EFF,
    CONF_DISCHARGE_LIMIT_ENTITY,
    CONF_ENERGY_TAX,
    CONF_EXPORT_ENERGY_ENTITY,
    CONF_EXPORT_FACTOR,
    CONF_EXPORT_FEE,
    CONF_EXPORT_FIXED,
    CONF_EXPORT_MODE,
    CONF_FEED_IN_LIMIT_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_IDLE_SETPOINT_W,
    CONF_IMPORT_ENERGY_ENTITY,
    CONF_LOAD_DAYS,
    CONF_LOAD_ENTITY,
    CONF_LOAD_FALLBACK_W,
    CONF_MARKUP,
    CONF_MAX_CHARGE_W,
    CONF_MAX_DISCHARGE_W,
    CONF_MAX_EXPORT_W,
    CONF_MAX_IMPORT_W,
    CONF_NETTING_END,
    CONF_NORDPOOL_AREA,
    CONF_NORDPOOL_ENTRY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_PRICE_INCLUDES_TAXES,
    CONF_PV_ENTITIES,
    CONF_PV_ESTIMATE,
    CONF_PV_LIMIT_ENTITY,
    CONF_PV_LIMIT_MAX_W,
    CONF_PV_LIMIT_UNIT,
    CONF_PV_POWER_ENTITY,
    CONF_PV_PRODUCTION_ENTITIES,
    CONF_PV_SCALE,
    CONF_SETPOINT_ENTITY,
    CONF_SOC_ENTITY,
    CONF_VAT,
    CONF_WEATHER_ENTITY,
    DEFAULTS,
    DOMAIN,
    NAME,
    PV_UNIT_PERCENT,
    PV_UNIT_WATT,
)
from .coordinator import state_float

STEPS = ("prices", "pv", "battery", "load", "control")

# Entity-id suffixes of the core `victron_gx` integration used as suggestions.
VICTRON_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    CONF_SETPOINT_ENTITY: ("number.", "_ac_power_setpoint"),
    CONF_CHARGE_LIMIT_ENTITY: ("number.", "_ess_max_charge_power_limit"),
    CONF_DISCHARGE_LIMIT_ENTITY: ("number.", "_ess_max_inverter_power_limit"),
    CONF_FEED_IN_LIMIT_ENTITY: ("number.", "_ess_max_feed_in_power"),
    CONF_DESS_MODE_ENTITY: ("select.", "_dess_mode"),
    CONF_SOC_ENTITY: ("sensor.", "_dc_battery_charge"),
    CONF_LOAD_ENTITY: ("sensor.", "_consumption_power_l1"),
    CONF_GRID_POWER_ENTITY: ("sensor.", "_grid_power_l1"),
    CONF_BATTERY_POWER_ENTITY: ("sensor.", "_dc_battery_power"),
    CONF_PV_POWER_ENTITY: ("sensor.", "_pv_on_grid_power_l1"),
}


def _has_live_l2(hass: HomeAssistant, l1_entity_id: str, ids: set[str]) -> bool:
    """True when the L2 sibling of an L1 sensor indicates a multi-phase system.

    The GX also exposes L2/L3 sensors on single-phase systems; they then read
    exactly 0. An L2 sensor without a state yet counts as multi-phase (safe side).
    """
    l2 = l1_entity_id.removesuffix("_l1") + "_l2"
    if l2 not in ids:
        return False
    return state_float(hass, l2) != 0.0


def victron_suggestions(hass: HomeAssistant) -> dict[str, str]:
    """Suggest entities from the core victron_gx integration, if present."""
    registry = er.async_get(hass)
    found: dict[str, str] = {}
    entries = [e for e in registry.entities.values() if e.platform == "victron_gx" and not e.disabled]
    ids = {e.entity_id for e in entries}
    for key, (prefix, suffix) in VICTRON_SUGGESTIONS.items():
        for entry in entries:
            if entry.entity_id.startswith(prefix) and entry.entity_id.endswith(suffix):
                # Per-phase sensors only describe the whole system on a single-phase
                # installation: on 3-phase systems leave them for the user to pick.
                if suffix.endswith("_l1") and _has_live_l2(hass, entry.entity_id, ids):
                    break
                found[key] = entry.entity_id
                break
    for entry in entries:
        if (
            entry.entity_id.startswith("number.")
            and "pvinverter" in (entry.unique_id or "")
            and entry.entity_id.endswith("_power_limit")
        ):
            found[CONF_PV_LIMIT_ENTITY] = entry.entity_id
            break
    return found


def _entity(domain: str | list[str], multiple: bool = False) -> sel.EntitySelector:
    return sel.EntitySelector(sel.EntitySelectorConfig(domain=domain, multiple=multiple))


def _num(min_: float, max_: float, step: float | str, unit: str | None = None) -> sel.NumberSelector:
    config = sel.NumberSelectorConfig(min=min_, max=max_, step=step, mode=sel.NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return sel.NumberSelector(config)


def _select(options: list[str], key: str) -> sel.SelectSelector:
    return sel.SelectSelector(
        sel.SelectSelectorConfig(options=options, translation_key=key, mode=sel.SelectSelectorMode.DROPDOWN)
    )


def _opt(key: str, values: dict[str, Any]) -> vol.Optional:
    """Optional field with a suggested value (works with empty defaults)."""
    value = values.get(key, DEFAULTS.get(key))
    if value is None or value == "":
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


def _req(key: str, values: dict[str, Any]) -> vol.Required:
    value = values.get(key, DEFAULTS.get(key))
    if value is None or value == "":
        return vol.Required(key)
    return vol.Required(key, default=value)


def step_schema(step: str, values: dict[str, Any]) -> vol.Schema:
    """Schema for a step, prefilled with `values`."""
    if step == "prices":
        return vol.Schema(
            {
                _opt(CONF_PRICE_ENTITY, values): _entity("sensor"),
                _opt(CONF_NORDPOOL_ENTRY, values): sel.ConfigEntrySelector(
                    sel.ConfigEntrySelectorConfig(integration="nordpool")
                ),
                _opt(CONF_NORDPOOL_AREA, values): sel.TextSelector(),
                _req(CONF_PRICE_INCLUDES_TAXES, values): sel.BooleanSelector(),
                _req(CONF_MARKUP, values): _num(-1, 1, "any", "EUR/kWh"),
                _req(CONF_ENERGY_TAX, values): _num(0, 1, "any", "EUR/kWh"),
                _req(CONF_VAT, values): _num(0, 50, 0.1, "%"),
                _req(CONF_EXPORT_MODE, values): _select(["netting", "spot", "fixed"], "export_mode"),
                _opt(CONF_NETTING_END, values): sel.DateSelector(),
                _req(CONF_EXPORT_FACTOR, values): _num(0, 2, 0.01),
                _req(CONF_EXPORT_FEE, values): _num(-1, 1, "any", "EUR/kWh"),
                _req(CONF_EXPORT_FIXED, values): _num(-1, 1, "any", "EUR/kWh"),
                _opt(CONF_WEATHER_ENTITY, values): _entity("weather"),
                _opt(CONF_PRICE_FORECAST_ENTITY, values): _entity("sensor"),
            }
        )
    if step == "pv":
        return vol.Schema(
            {
                _opt(CONF_PV_ENTITIES, values): _entity("sensor", multiple=True),
                _req(CONF_PV_ESTIMATE, values): _select(
                    ["pv_estimate", "pv_estimate10", "pv_estimate90"], "pv_estimate"
                ),
                _req(CONF_PV_SCALE, values): _num(0, 3, 0.01),
                _opt(CONF_PV_PRODUCTION_ENTITIES, values): _entity("sensor", multiple=True),
            }
        )
    if step == "battery":
        return vol.Schema(
            {
                _req(CONF_SOC_ENTITY, values): _entity("sensor"),
                _req(CONF_CAPACITY_KWH, values): _num(0.5, 200, 0.1, "kWh"),
                _req(CONF_MAX_CHARGE_W, values): _num(100, 50000, 50, "W"),
                _req(CONF_MAX_DISCHARGE_W, values): _num(100, 50000, 50, "W"),
                _req(CONF_CHARGE_EFF, values): _num(50, 100, 0.5, "%"),
                _req(CONF_DISCHARGE_EFF, values): _num(50, 100, 0.5, "%"),
                _opt(CONF_BATTERY_POWER_ENTITY, values): _entity("sensor"),
            }
        )
    if step == "load":
        return vol.Schema(
            {
                _opt(CONF_LOAD_ENTITY, values): _entity("sensor"),
                _req(CONF_LOAD_DAYS, values): _num(1, 60, 1, "d"),
                _req(CONF_LOAD_FALLBACK_W, values): _num(0, 20000, 10, "W"),
                _opt(CONF_GRID_POWER_ENTITY, values): _entity("sensor"),
                _opt(CONF_IMPORT_ENERGY_ENTITY, values): _entity("sensor"),
                _opt(CONF_EXPORT_ENERGY_ENTITY, values): _entity("sensor"),
                _req(CONF_MAX_IMPORT_W, values): _num(0, 100000, 100, "W"),
                _req(CONF_MAX_EXPORT_W, values): _num(0, 100000, 100, "W"),
            }
        )
    if step == "control":
        return vol.Schema(
            {
                _opt(CONF_SETPOINT_ENTITY, values): _entity("number"),
                _opt(CONF_CHARGE_LIMIT_ENTITY, values): _entity("number"),
                _opt(CONF_DISCHARGE_LIMIT_ENTITY, values): _entity("number"),
                _opt(CONF_FEED_IN_LIMIT_ENTITY, values): _entity("number"),
                _opt(CONF_DESS_MODE_ENTITY, values): _entity("select"),
                _req(CONF_IDLE_SETPOINT_W, values): _num(-1000, 1000, 5, "W"),
                _opt(CONF_PV_LIMIT_ENTITY, values): _entity("number"),
                _req(CONF_PV_LIMIT_UNIT, values): _select([PV_UNIT_WATT, PV_UNIT_PERCENT], "pv_limit_unit"),
                _req(CONF_PV_LIMIT_MAX_W, values): _num(0, 100000, 10, "W"),
                _opt(CONF_PV_POWER_ENTITY, values): _entity("sensor"),
            }
        )
    raise ValueError(step)


def validate_step(hass: HomeAssistant, step: str, user_input: dict[str, Any]) -> dict[str, str]:
    """Return form errors for a step."""
    errors: dict[str, str] = {}
    if step == "prices":
        if user_input.get(CONF_NORDPOOL_ENTRY):
            entry = hass.config_entries.async_get_entry(user_input[CONF_NORDPOOL_ENTRY])
            if entry is None or entry.domain != "nordpool" or not user_input.get(CONF_NORDPOOL_AREA):
                errors[CONF_NORDPOOL_ENTRY] = "invalid_nordpool"
            return errors
        if not user_input.get(CONF_PRICE_ENTITY):
            return {CONF_PRICE_ENTITY: "entity_not_found"}
        state = hass.states.get(user_input[CONF_PRICE_ENTITY])
        if state is None:
            errors[CONF_PRICE_ENTITY] = "entity_not_found"
        else:
            from .providers.prices import parse_price_attributes

            try:
                if not parse_price_attributes(state.attributes):
                    errors[CONF_PRICE_ENTITY] = "no_price_forecast"
            except ValueError:
                errors[CONF_PRICE_ENTITY] = "invalid_price_unit"
    return errors


class EnergyControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Init."""
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Entry point."""
        if not self._data:
            self._data = victron_suggestions(self.hass)
        return await self.async_step_prices(user_input)

    async def _step(self, step: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_step(self.hass, step, user_input)
            if not errors:
                self._data.update(user_input)
                index = STEPS.index(step)
                if index + 1 < len(STEPS):
                    return await getattr(self, f"async_step_{STEPS[index + 1]}")()
                return self.async_create_entry(title=NAME, data=self._data)
        return self.async_show_form(
            step_id=step,
            data_schema=step_schema(step, {**self._data, **(user_input or {})}),
            errors=errors,
            last_step=step == STEPS[-1],
        )

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Prices."""
        return await self._step("prices", user_input)

    async def async_step_pv(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """PV forecast."""
        return await self._step("pv", user_input)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Battery."""
        return await self._step("battery", user_input)

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Load & grid."""
        return await self._step("load", user_input)

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Hardware control."""
        return await self._step("control", user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return EnergyControlOptionsFlow()


class EnergyControlOptionsFlow(OptionsFlow):
    """Edit any step after setup (menu)."""

    def __init__(self) -> None:
        """Init."""
        self._values: dict[str, Any] | None = None

    @property
    def values(self) -> dict[str, Any]:
        """Current merged configuration being edited."""
        if self._values is None:
            # Options hold the complete configuration once saved: merging with
            # data would resurrect fields cleared in an earlier options edit.
            self._values = dict(self.config_entry.options or self.config_entry.data)
        return self._values

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu."""
        return self.async_show_menu(step_id="init", menu_options=STEPS)

    async def _step(self, step: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_step(self.hass, step, user_input)
            if not errors:
                # Optional entity fields cleared in the form are absent: drop them.
                for key in step_schema(step, {}).schema:
                    self.values.pop(str(key), None)
                self.values.update(user_input)
                # Save on submit: a menu-level "save" was easy to miss, and
                # closing the dialog then silently discarded the edit.
                return self.async_create_entry(data=self.values)
        return self.async_show_form(
            step_id=step, data_schema=step_schema(step, {**self.values, **(user_input or {})}), errors=errors
        )

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Prices."""
        return await self._step("prices", user_input)

    async def async_step_pv(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """PV forecast."""
        return await self._step("pv", user_input)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Battery."""
        return await self._step("battery", user_input)

    async def async_step_load(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Load & grid."""
        return await self._step("load", user_input)

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Hardware control."""
        return await self._step("control", user_input)
