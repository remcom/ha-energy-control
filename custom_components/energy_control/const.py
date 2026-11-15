"""Constants for Energy Control."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "energy_control"
NAME: Final = "Energy Control"
VERSION: Final = "0.1.0"

# --- Config entry keys -------------------------------------------------------
# Prices
CONF_PRICE_ENTITY: Final = "price_entity"
CONF_PRICE_INCLUDES_TAXES: Final = "price_includes_taxes"
CONF_MARKUP: Final = "markup"
CONF_ENERGY_TAX: Final = "energy_tax"
CONF_VAT: Final = "vat"
CONF_EXPORT_MODE: Final = "export_mode"
CONF_NETTING_END: Final = "netting_end"
CONF_EXPORT_FACTOR: Final = "export_factor"
CONF_EXPORT_FEE: Final = "export_fee"
CONF_EXPORT_FIXED: Final = "export_fixed"
# PV
CONF_PV_ENTITIES: Final = "pv_entities"
CONF_PV_ESTIMATE: Final = "pv_estimate"
CONF_PV_SCALE: Final = "pv_scale"
# Battery
CONF_SOC_ENTITY: Final = "soc_entity"
CONF_CAPACITY_KWH: Final = "capacity_kwh"
CONF_MAX_CHARGE_W: Final = "max_charge_w"
CONF_MAX_DISCHARGE_W: Final = "max_discharge_w"
CONF_CHARGE_EFF: Final = "charge_efficiency"
CONF_DISCHARGE_EFF: Final = "discharge_efficiency"
# Load & grid
CONF_LOAD_ENTITY: Final = "load_entity"
CONF_LOAD_DAYS: Final = "load_days"
CONF_LOAD_FALLBACK_W: Final = "load_fallback_w"
CONF_GRID_POWER_ENTITY: Final = "grid_power_entity"
CONF_BATTERY_POWER_ENTITY: Final = "battery_power_entity"
CONF_MAX_IMPORT_W: Final = "max_import_w"
CONF_MAX_EXPORT_W: Final = "max_export_w"
# Control (Victron ESS + PV inverter limiter)
CONF_SETPOINT_ENTITY: Final = "setpoint_entity"
CONF_CHARGE_LIMIT_ENTITY: Final = "charge_limit_entity"
CONF_DISCHARGE_LIMIT_ENTITY: Final = "discharge_limit_entity"
CONF_FEED_IN_LIMIT_ENTITY: Final = "feed_in_limit_entity"
CONF_DESS_MODE_ENTITY: Final = "dess_mode_entity"
CONF_IDLE_SETPOINT_W: Final = "idle_setpoint_w"
CONF_PV_LIMIT_ENTITY: Final = "pv_limit_entity"
CONF_PV_LIMIT_UNIT: Final = "pv_limit_unit"
CONF_PV_LIMIT_MAX_W: Final = "pv_limit_max_w"
CONF_PV_POWER_ENTITY: Final = "pv_power_entity"

DEFAULTS: Final[dict[str, object]] = {
    CONF_PRICE_INCLUDES_TAXES: True,
    CONF_MARKUP: 0.0,
    CONF_ENERGY_TAX: 0.0,
    CONF_VAT: 21.0,
    CONF_EXPORT_MODE: "netting",
    CONF_NETTING_END: "2027-01-01",
    CONF_EXPORT_FACTOR: 1.0,
    CONF_EXPORT_FEE: 0.0,
    CONF_EXPORT_FIXED: 0.0,
    CONF_PV_ESTIMATE: "pv_estimate",
    CONF_PV_SCALE: 1.0,
    CONF_CAPACITY_KWH: 10.0,
    CONF_MAX_CHARGE_W: 3000,
    CONF_MAX_DISCHARGE_W: 3000,
    CONF_CHARGE_EFF: 95.0,
    CONF_DISCHARGE_EFF: 95.0,
    CONF_LOAD_DAYS: 14,
    CONF_LOAD_FALLBACK_W: 400,
    CONF_MAX_IMPORT_W: 8000,
    CONF_MAX_EXPORT_W: 4600,
    CONF_IDLE_SETPOINT_W: 30,
    CONF_PV_LIMIT_UNIT: "W",
    CONF_PV_LIMIT_MAX_W: 2700,
}

# --- Runtime settings (entities, persisted in a Store) -----------------------
SET_MODE: Final = "mode"
SET_GRID_CHARGE: Final = "grid_charge"
SET_BATTERY_TO_GRID: Final = "battery_to_grid"
SET_CURTAIL_NEGATIVE: Final = "curtail_negative"
SET_MIN_SOC: Final = "min_soc"
SET_MAX_SOC: Final = "max_soc"
SET_CYCLE_COST: Final = "cycle_cost"  # ct/kWh discharged
SET_ARBITRAGE_MARGIN: Final = "arbitrage_margin"  # ct/kWh


class Mode(StrEnum):
    """Operating mode of the integration."""

    OFF = "off"  # do nothing, hardware restored to original settings
    ADVISORY = "advisory"  # plan only, never write to hardware
    AUTO = "auto"  # execute the optimized plan
    SELF_CONSUMPTION = "self_consumption"  # plain ESS self consumption
    FORCE_CHARGE = "force_charge"
    FORCE_DISCHARGE = "force_discharge"
    HOLD = "hold"


CONTROL_MODES: Final = {
    Mode.AUTO,
    Mode.SELF_CONSUMPTION,
    Mode.FORCE_CHARGE,
    Mode.FORCE_DISCHARGE,
    Mode.HOLD,
}

DEFAULT_SETTINGS: Final[dict[str, object]] = {
    SET_MODE: Mode.ADVISORY.value,
    SET_GRID_CHARGE: True,
    SET_BATTERY_TO_GRID: False,
    SET_CURTAIL_NEGATIVE: True,
    SET_MIN_SOC: 15.0,
    SET_MAX_SOC: 100.0,
    SET_CYCLE_COST: 2.0,
    SET_ARBITRAGE_MARGIN: 1.0,
}

# --- Timing ------------------------------------------------------------------
HORIZON_HOURS: Final = 48
CONTROL_INTERVAL_S: Final = 10
STALE_PLAN_MINUTES: Final = 45
LOAD_PROFILE_REFRESH_H: Final = 6
HISTORY_DAYS: Final = 7

SIGNAL_PLAN_UPDATED: Final = f"{DOMAIN}_plan_updated_{{}}"

# --- Frontend ----------------------------------------------------------------
FRONTEND_URL: Final = f"/{DOMAIN}/energy-control-card.js"
