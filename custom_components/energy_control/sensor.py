"""Sensors exposing the plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .data import EnergyControlConfigEntry, EnergyControlData
from .entity import EnergyControlEntity
from .optimizer import Action, Plan, SlotPlan


def _current(data: EnergyControlData) -> SlotPlan | None:
    plan = data.coordinator.data
    return plan.slot_at(dt_util.utcnow()) if plan else None


def _next_start(plan: Plan | None, action: Action) -> datetime | None:
    if plan is None:
        return None
    now = dt_util.utcnow()
    current = plan.slot_at(now)
    if current is not None and current.action == action:
        return current.start
    for slot in plan.slots:
        if slot.action == action and slot.start >= now:
            return slot.start
    return None


def _today_start() -> datetime:
    return dt_util.start_of_local_day()


@dataclass(frozen=True, kw_only=True)
class PlanSensorDescription(SensorEntityDescription):
    """Sensor computed from runtime data."""

    value_fn: Callable[[EnergyControlData], Any]


SENSORS: tuple[PlanSensorDescription, ...] = (
    PlanSensorDescription(
        key="action",
        device_class=SensorDeviceClass.ENUM,
        options=[a.value for a in Action],
        value_fn=lambda d: s.action.value if (s := _current(d)) else None,
    ),
    PlanSensorDescription(
        key="planned_grid_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(s.grid_kw * 1000) if (s := _current(d)) else None,
    ),
    PlanSensorDescription(
        key="planned_battery_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(s.battery_kw * 1000) if (s := _current(d)) else None,
    ),
    PlanSensorDescription(
        key="target_soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: round(s.soc_end_pct, 1) if (s := _current(d)) else None,
    ),
    PlanSensorDescription(
        key="next_grid_charge",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: _next_start(d.coordinator.data, Action.CHARGE_GRID),
    ),
    PlanSensorDescription(
        key="next_grid_discharge",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: _next_start(d.coordinator.data, Action.DISCHARGE_GRID),
    ),
    PlanSensorDescription(
        key="plan_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: round(p.total_cost, 3) if (p := d.coordinator.data) and p.export_prices_complete else None,
    ),
    PlanSensorDescription(
        key="plan_savings",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: round(p.savings, 3) if (p := d.coordinator.data) and p.export_prices_complete else None,
    ),
    PlanSensorDescription(
        key="pv_forecast_accuracy",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: a["accuracy"] if (a := d.history.forecast_accuracy(dt_util.utcnow(), "pv")) else None,
    ),
    PlanSensorDescription(
        key="load_forecast_accuracy",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: a["accuracy"] if (a := d.history.forecast_accuracy(dt_util.utcnow(), "load")) else None,
    ),
    PlanSensorDescription(
        key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: (
            round(cost, 3) if (cost := d.history.actual_cost_since(_today_start())) is not None else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergyControlConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Set up sensors."""
    data = entry.runtime_data
    entities: list[SensorEntity] = [PlanValueSensor(data, d) for d in SENSORS]
    entities.append(PlanSensor(data))
    if data.coordinator.flex.hp_configured:
        entities.append(HeatPumpSensor(data))
    if data.coordinator.flex.ev_configured:
        entities.append(EvSensor(data))
    async_add_entities(entities)


class PlanValueSensor(EnergyControlEntity, SensorEntity):
    """Value derived from the plan / history."""

    entity_description: PlanSensorDescription

    def __init__(self, data: EnergyControlData, description: PlanSensorDescription) -> None:
        """Init."""
        super().__init__(data, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Value."""
        return self.entity_description.value_fn(self.data)

    @property
    def last_reset(self) -> datetime | None:
        """Daily reset for the cost-today sensor."""
        return _today_start() if self.entity_description.key == "cost_today" else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Reason for the current action."""
        if self.entity_description.key == "cost_today":
            return {
                "coverage": self.data.history.coverage_since(_today_start()),
                "observed_cost": self.data.history.observed_cost_since(_today_start()),
            }
        if (key := self.entity_description.key).endswith("_forecast_accuracy"):
            kind = key.removesuffix("_forecast_accuracy")
            details = self.data.history.forecast_accuracy(dt_util.utcnow(), kind) or {}
            return {k: v for k, v in details.items() if k != "accuracy"}
        if self.entity_description.key != "action":
            return None
        slot = _current(self.data)
        return {
            "reason": slot.reason if slot else None,
            "mode": self.coordinator.mode.value,
            "status": self.data.controller.status,
        }


class PlanSensor(EnergyControlEntity, SensorEntity):
    """Plan timestamp; compact schedule in (unrecorded) attributes."""

    _unrecorded_attributes = frozenset({"forecast", "summary"})
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, data: EnergyControlData) -> None:
        """Init."""
        super().__init__(data, "plan")

    @property
    def native_value(self) -> datetime | None:
        """When the plan was made."""
        plan = self.coordinator.data
        return plan.created if plan else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Compact schedule for templates and apexcharts."""
        plan = self.coordinator.data
        if plan is None:
            return {"error": self.coordinator.last_plan_error}
        return {
            "summary": {
                "slots": len(plan.slots),
                "feasible": plan.feasible,
                "export_prices_complete": plan.export_prices_complete,
                "horizon_end": plan.slots[-1].start.isoformat() if plan.slots else None,
                "cost": round(plan.total_cost, 3) if plan.export_prices_complete else None,
                "baseline_cost": round(plan.baseline_cost, 3) if plan.export_prices_complete else None,
                "wear_cost": round(plan.wear_cost, 3),
                "savings": round(plan.savings, 3) if plan.export_prices_complete else None,
            },
            "forecast": [
                {
                    "start": s.start.isoformat(),
                    "action": s.action.value,
                    "battery_w": round(s.battery_kw * 1000),
                    "grid_w": round(s.grid_kw * 1000),
                    "soc": round(s.soc_end_pct, 1),
                    "price": round(s.import_price, 4),
                }
                for s in plan.slots
            ],
            "error": self.coordinator.last_plan_error,
        }


class HeatPumpSensor(EnergyControlEntity, SensorEntity):
    """Commanded SG Ready state, with why and what is planned next."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["normal", "block", "comfort", "boost", "off"]
    _unrecorded_attributes = frozenset({"schedule"})

    def __init__(self, data: EnergyControlData) -> None:
        """Init."""
        super().__init__(data, "heat_pump_mode")

    @property
    def native_value(self) -> str:
        """Mode now (off = not controlled)."""
        flex = self.coordinator.flex
        return flex.hp_mode.value if flex.hp_active and flex.hp_mode else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Status, block bookkeeping and the planned blocks."""
        flex = self.coordinator.flex
        now = dt_util.utcnow()
        planned = sorted((start, mode) for start, mode in flex.hp_modes.items() if mode != "normal")
        nxt = next(((start, mode) for start, mode in planned if start >= now), None)
        return {
            "status": flex.hp_status,
            "blocked_today_h": round(flex.blocked_today_h(now), 2),
            "next_change": nxt[0].isoformat() if nxt else None,
            "next_mode": nxt[1].value if nxt else None,
            "expected_power_kw": round(flex.hp_power(now), 3) if flex.hp_model else None,
            "model_w_per_degree": round(flex.hp_model.b * 1000) if flex.hp_model else None,
            "model_no_heating_above_c": (
                round(t, 1) if flex.hp_model and (t := flex.hp_model.balance_temp) is not None else None
            ),
            "model_days": flex.hp_model.days if flex.hp_model else None,
            "model_r2": round(flex.hp_model.r2, 2) if flex.hp_model and flex.hp_model.r2 is not None else None,
            "schedule": [{"start": start.isoformat(), "mode": mode.value} for start, mode in planned],
        }


class EvSensor(EnergyControlEntity, SensorEntity):
    """EV charging status and next planned charge."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["manual", "waiting", "charging", "done", "unavailable", "off"]

    def __init__(self, data: EnergyControlData) -> None:
        """Init."""
        super().__init__(data, "ev_charging")

    @property
    def native_value(self) -> str:
        """Charging status."""
        return self.coordinator.flex.ev_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Need, next charge quarter and ready-by."""
        flex = self.coordinator.flex
        now = dt_util.utcnow()
        upcoming = sorted(s for s in flex.ev_slots if s >= now - timedelta(minutes=15))
        return {
            "energy_needed_kwh": round(flex.ev_need_kwh(), 2),
            "next_charge": upcoming[0].isoformat() if upcoming else None,
            "planned_quarters": len(upcoming),
            "ready_by": flex.ev_ready_by.isoformat(),
        }
