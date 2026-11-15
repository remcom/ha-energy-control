"""Victron ESS controller: executes the plan via Home Assistant entities.

Writes only on change. On first takeover the original actuator values are
saved (persistently) and restored when the mode is set to off/advisory, the
entry is unloaded, or Home Assistant stops.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BATTERY_POWER_ENTITY,
    CONF_CHARGE_LIMIT_ENTITY,
    CONF_DESS_MODE_ENTITY,
    CONF_DISCHARGE_EFF,
    CONF_DISCHARGE_LIMIT_ENTITY,
    CONF_FEED_IN_LIMIT_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_IDLE_SETPOINT_W,
    CONF_MAX_CHARGE_W,
    CONF_MAX_DISCHARGE_W,
    CONF_MAX_EXPORT_W,
    CONF_MAX_IMPORT_W,
    CONF_PV_LIMIT_ENTITY,
    CONF_PV_LIMIT_MAX_W,
    CONF_PV_LIMIT_UNIT,
    CONF_PV_POWER_ENTITY,
    CONF_SETPOINT_ENTITY,
    CONF_SOC_ENTITY,
    CONTROL_INTERVAL_S,
    CONTROL_MODES,
    DOMAIN,
    SET_CURTAIL_NEGATIVE,
    SET_MAX_SOC,
    SET_MIN_SOC,
    STALE_PLAN_MINUTES,
    Mode,
)
from ..coordinator import EnergyControlCoordinator, state_float
from ..history import History
from ..optimizer import Action, SlotPlan
from .base import Desired, Limits, Originals, desired_for
from .pv_limiter import curtail_limit_w, from_entity_value, to_entity_value

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DESS_OFF_OPTIONS = ("off", "disabled", "0")
WRITE_TOLERANCE_W = 5.0
PV_LIMIT_HYSTERESIS_W = 40.0
REPLAN_SOC_DEVIATION = 5.0  # % points
STORE_CURTAILING = "_curtailing"  # extra key in the originals store

# Actuators that must report a value before we take over, so their original
# setting can be saved and restored. The PV limiter is excluded: many PV
# inverters go offline at night; its original is captured once it reports.
TAKEOVER_KEYS = (
    CONF_SETPOINT_ENTITY,
    CONF_CHARGE_LIMIT_ENTITY,
    CONF_DISCHARGE_LIMIT_ENTITY,
    CONF_FEED_IN_LIMIT_ENTITY,
)


class VictronController:
    """Applies the current plan slot to a Victron ESS (+ optional PV limiter)."""

    def __init__(self, hass: HomeAssistant, coordinator: EnergyControlCoordinator, history: History) -> None:
        """Init."""
        self.hass = hass
        self.coordinator = coordinator
        self.history = history
        self.config = coordinator.config
        entry_id = coordinator.config_entry.entry_id
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.originals")
        self._originals: dict[str, Any] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_stop: Callable[[], None] | None = None
        self._stopping = False
        self._waiting_logged = False
        self.active = False  # we currently own the hardware
        self.desired: Desired | None = None
        self.curtailing = False
        self.status = "idle"
        self.last_error: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------
    @property
    def can_control(self) -> bool:
        """True when at least the grid setpoint entity is configured."""
        return bool(self.config.get(CONF_SETPOINT_ENTITY))

    async def async_start(self) -> None:
        """Start the control loop."""
        stored = await self._store.async_load()
        if stored is not None:
            # Crashed while in control: we still own the hardware.
            stored = dict(stored)
            self.curtailing = bool(stored.pop(STORE_CURTAILING, False))
            self._originals = stored
            self.active = True
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=CONTROL_INTERVAL_S)))
        self._unsubs.append(self.coordinator.async_add_listener(self._on_update))
        self._unsub_stop = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop)
        await self.async_apply()

    def _cancel_listeners(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None

    async def async_stop(self) -> None:
        """Stop and hand control back."""
        self._stopping = True
        self._cancel_listeners()
        await self.async_release()

    async def _on_stop(self, _event: Event) -> None:
        # Best effort: never leave the ESS in a forced charge/discharge state.
        # The once-listener is gone after firing: do not remove it again.
        self._unsub_stop = None
        self._stopping = True
        self._cancel_listeners()
        await self.async_release()

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Notify entities about controller state changes."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def _on_update(self) -> None:
        if not self._stopping:
            self.hass.async_create_task(self.async_apply())

    async def _tick(self, now: datetime) -> None:
        self._record(now)
        await self.async_apply()

    # --- history -----------------------------------------------------------
    def _record(self, now: datetime) -> None:
        plan = self.coordinator.data
        planned = plan.slot_at(now) if plan else None
        soc = state_float(self.hass, self.config.get(CONF_SOC_ENTITY))
        self.history.sample(
            now,
            mode=str(self.coordinator.mode),
            planned=planned,
            grid_w=state_float(self.hass, self.config.get(CONF_GRID_POWER_ENTITY)),
            battery_w=state_float(self.hass, self.config.get(CONF_BATTERY_POWER_ENTITY)),
            soc_pct=soc,
            curtailing=self.curtailing,
        )
        # Replan when reality drifts away from the plan.
        if planned is not None and soc is not None:
            frac = (now - planned.start).total_seconds() / (planned.hours * 3600 or 1)
            expected = planned.soc_start_pct + (planned.soc_end_pct - planned.soc_start_pct) * frac
            if abs(soc - expected) > REPLAN_SOC_DEVIATION:
                self.hass.async_create_task(self.coordinator.async_request_refresh())

    # --- decision ----------------------------------------------------------
    def _limits(self) -> Limits:
        c = self.config
        return Limits(
            idle_setpoint=float(c[CONF_IDLE_SETPOINT_W]),
            max_import=float(c[CONF_MAX_IMPORT_W]),
            max_export=float(c[CONF_MAX_EXPORT_W]),
            max_charge=float(c[CONF_MAX_CHARGE_W]),
            max_discharge=float(c[CONF_MAX_DISCHARGE_W]),
            discharge_efficiency=float(c[CONF_DISCHARGE_EFF]) / 100,
        )

    def _originals_obj(self) -> Originals:
        o = self._originals or {}
        return Originals(
            charge_limit=float(o.get(CONF_CHARGE_LIMIT_ENTITY, -1)),
            discharge_limit=float(o.get(CONF_DISCHARGE_LIMIT_ENTITY, -1)),
            feed_in_limit=float(o.get(CONF_FEED_IN_LIMIT_ENTITY, -1)),
            pv_limit_w=o.get(CONF_PV_LIMIT_ENTITY),
        )

    def decide(self, now: datetime) -> tuple[Action, SlotPlan | None, str]:
        """Action for `now`, the plan slot used (if any) and a status string."""
        coordinator = self.coordinator
        mode = coordinator.mode
        soc = state_float(self.hass, self.config.get(CONF_SOC_ENTITY))
        min_soc = float(coordinator.settings[SET_MIN_SOC])
        max_soc = max(float(coordinator.settings[SET_MAX_SOC]), min_soc)

        def forced(action: Action, status: str) -> tuple[Action, SlotPlan | None, str]:
            # Manual forcing (mode or override) still respects the SoC window.
            if action == Action.CHARGE_GRID and soc is not None and soc >= max_soc:
                return Action.SELF_CONSUME, None, "max_soc_reached"
            if action == Action.DISCHARGE_GRID and soc is not None and soc <= min_soc:
                return Action.SELF_CONSUME, None, "min_soc_reached"
            return action, None, status

        override = coordinator.override
        if override is not None:
            if now < override.until:
                return forced(override.action, "override")
            coordinator.override = None

        if mode == Mode.SELF_CONSUMPTION:
            return Action.SELF_CONSUME, None, "manual"
        if mode == Mode.HOLD:
            return Action.HOLD, None, "manual"
        if mode == Mode.FORCE_CHARGE:
            return forced(Action.CHARGE_GRID, "manual")
        if mode == Mode.FORCE_DISCHARGE:
            return forced(Action.DISCHARGE_GRID, "manual")

        plan = coordinator.data
        if plan is None or now - plan.created > timedelta(minutes=STALE_PLAN_MINUTES):
            return Action.SELF_CONSUME, None, "fallback_stale_plan"
        if soc is None:
            return Action.SELF_CONSUME, None, "fallback_no_soc"
        slot = plan.slot_at(now)
        if slot is None:
            return Action.SELF_CONSUME, None, "fallback_no_slot"
        return slot.action, slot, "ok"

    # --- apply -------------------------------------------------------------
    async def async_apply(self) -> None:
        """Bring the hardware in line with the current decision."""
        async with self._lock:
            await self._async_apply()

    async def _async_apply(self) -> None:
        if self._stopping:
            return
        mode = self.coordinator.mode
        if mode not in CONTROL_MODES or not self.can_control:
            if self.active:
                await self._async_release()
            self.status = "advisory" if mode == Mode.ADVISORY else "off"
            self._notify()
            return

        now = dt_util.utcnow()
        action, slot, status = self.decide(now)
        override = self.coordinator.override
        limits = self._limits()
        if slot is not None:
            battery_kw, grid_kw = slot.battery_kw, slot.grid_kw
            curtail = slot.curtail_kw > 1e-3 and (slot.export_price < 0 or slot.import_price < 0)
        else:
            power = override.power_w if override and override.power_w else None
            battery_kw = (power or limits.max_charge) / 1000 if action == Action.CHARGE_GRID else 0.0
            if action == Action.DISCHARGE_GRID:
                # A requested override power is the AC inverter limit; desired_for
                # expects battery-side (DC) power.
                battery_kw = -(power / limits.discharge_efficiency if power else limits.max_discharge) / 1000
            grid_kw = 0.0
            curtail = False
        if not self.coordinator.settings.get(SET_CURTAIL_NEGATIVE, True):
            curtail = False

        if not await self._async_take_over():
            self.status = "waiting_for_entities"
            self._notify()
            return
        try:
            desired = desired_for(
                action,
                battery_kw=battery_kw,
                grid_kw=grid_kw,
                curtail=curtail,
                originals=self._originals_obj(),
                limits=limits,
            )
            await self._write_number(CONF_SETPOINT_ENTITY, desired.setpoint)
            await self._write_number(CONF_CHARGE_LIMIT_ENTITY, desired.charge_limit)
            await self._write_number(CONF_DISCHARGE_LIMIT_ENTITY, desired.discharge_limit)
            await self._write_number(CONF_FEED_IN_LIMIT_ENTITY, desired.feed_in_limit)
            await self._apply_pv_limit(desired, slot)
            self.desired = desired
            self.status = status
            self.last_error = None
            ir.async_delete_issue(self.hass, DOMAIN, "control_failed")
        except HomeAssistantError as err:
            self.status = "error"
            self.last_error = str(err)
            _LOGGER.warning("Energy Control could not apply %s: %s", action, err)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "control_failed",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="control_failed",
                translation_placeholders={"error": str(err)},
            )
        self._notify()

    def _missing_entities(self) -> list[str]:
        """Configured actuators that do not report a usable value yet."""
        missing = []
        for key in TAKEOVER_KEYS:
            entity_id = self.config.get(key)
            if entity_id and state_float(self.hass, entity_id) is None:
                missing.append(entity_id)
        dess = self.config.get(CONF_DESS_MODE_ENTITY)
        if dess:
            state = self.hass.states.get(dess)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                missing.append(dess)
        return missing

    def _pv_limit_original(self) -> float | None:
        pv_value = state_float(self.hass, self.config.get(CONF_PV_LIMIT_ENTITY))
        if pv_value is None:
            return None
        return from_entity_value(pv_value, self.config[CONF_PV_LIMIT_UNIT], float(self.config[CONF_PV_LIMIT_MAX_W]))

    async def _async_save_store(self) -> None:
        if self._originals is not None:
            await self._store.async_save({**self._originals, STORE_CURTAILING: self.curtailing})

    async def _async_take_over(self) -> bool:
        """Save the original settings and take control. False while not possible yet."""
        if self.active and self._originals is not None:
            # Late capture of the PV limiter original (inverter was offline).
            if (
                self.config.get(CONF_PV_LIMIT_ENTITY)
                and CONF_PV_LIMIT_ENTITY not in self._originals
                and not self.curtailing
                and (pv_original := self._pv_limit_original()) is not None
            ):
                self._originals[CONF_PV_LIMIT_ENTITY] = pv_original
                await self._async_save_store()
            return True
        if missing := self._missing_entities():
            if not self._waiting_logged:
                _LOGGER.warning("Energy Control waits for %s before taking over", ", ".join(missing))
                self._waiting_logged = True
            return False
        self._waiting_logged = False
        originals: dict[str, Any] = {}
        for key in TAKEOVER_KEYS:
            value = state_float(self.hass, self.config.get(key))
            if value is not None:
                originals[key] = value
        if (pv_original := self._pv_limit_original()) is not None:
            originals[CONF_PV_LIMIT_ENTITY] = pv_original
        dess = self.config.get(CONF_DESS_MODE_ENTITY)
        if dess and (state := self.hass.states.get(dess)) is not None:
            originals[CONF_DESS_MODE_ENTITY] = state.state
        self._originals = originals
        self.curtailing = False
        await self._async_save_store()
        self.active = True
        _LOGGER.info("Energy Control took over ESS control (saved %s)", originals)
        try:
            await self._set_dess_off()
        except HomeAssistantError as err:
            _LOGGER.warning("Energy Control could not switch DESS off: %s", err)
        return True

    async def async_release(self) -> None:
        """Restore the original actuator values."""
        async with self._lock:
            await self._async_release()

    async def _async_release(self) -> None:
        if not self.active:
            return
        originals = self._originals or {}
        errors: list[str] = []
        # Best effort per entity: one failure must not block the other restores.
        for key in TAKEOVER_KEYS:
            if key in originals:
                try:
                    await self._write_number(key, float(originals[key]))
                except HomeAssistantError as err:
                    errors.append(str(err))
        if self.config.get(CONF_PV_LIMIT_ENTITY) and (CONF_PV_LIMIT_ENTITY in originals or self.curtailing):
            pv_original = originals.get(CONF_PV_LIMIT_ENTITY, self.config[CONF_PV_LIMIT_MAX_W])
            try:
                await self._write_pv_limit(float(pv_original), force=True)
            except HomeAssistantError as err:
                errors.append(str(err))
        dess = self.config.get(CONF_DESS_MODE_ENTITY)
        if dess and originals.get(CONF_DESS_MODE_ENTITY):
            try:
                await self._select(dess, originals[CONF_DESS_MODE_ENTITY])
            except HomeAssistantError as err:
                errors.append(str(err))
        if errors:
            # Keep the saved originals: the next apply (or restart) retries.
            _LOGGER.error("Energy Control could not restore all original ESS settings: %s", "; ".join(errors))
            return
        self.active = False
        self.curtailing = False
        self.desired = None
        self._originals = None
        await self._store.async_remove()
        _LOGGER.info("Energy Control released ESS control")

    # --- writers -----------------------------------------------------------
    async def _set_dess_off(self) -> None:
        dess = self.config.get(CONF_DESS_MODE_ENTITY)
        if not dess or (state := self.hass.states.get(dess)) is None:
            return
        options = state.attributes.get("options") or []
        target = next((o for o in DESS_OFF_OPTIONS if o in options), None)
        if target is None:
            _LOGGER.warning("No 'off' option in %s (%s); leaving DESS mode", dess, options)
            return
        if state.state != target:
            await self._select(dess, target)

    async def _select(self, entity_id: str, option: str) -> None:
        state = self.hass.states.get(entity_id)
        if state is not None and state.state == option:
            return
        await self.hass.services.async_call(
            "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True
        )

    async def _write_number(self, key: str, value: float) -> None:
        entity_id = self.config.get(key)
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if state is None:
            raise HomeAssistantError(f"{entity_id} not found")
        # `value` is in W; min/max and the written value are in the entity's unit.
        factor = 1000.0 if state.attributes.get("unit_of_measurement") == "kW" else 1.0
        value /= factor
        lo = state.attributes.get("min")
        hi = state.attributes.get("max")
        if lo is not None:
            value = max(float(lo), value)
        if hi is not None:
            value = min(float(hi), value)
        current = state_float(self.hass, entity_id)
        if current is not None and abs(current - value * factor) <= WRITE_TOLERANCE_W:
            return
        _LOGGER.debug("Set %s: %s -> %s", entity_id, current, value)
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
        )

    async def _apply_pv_limit(self, desired: Desired, slot: SlotPlan | None) -> None:
        if not self.config.get(CONF_PV_LIMIT_ENTITY):
            self.curtailing = desired.curtail
            return
        max_w = float(self.config[CONF_PV_LIMIT_MAX_W])
        if desired.curtail:
            if slot is not None and slot.import_price < 0:
                limit = 0.0  # importing is paid: produce nothing
            else:
                fallback = 0.0
                if slot is not None:
                    fallback = (slot.load_kw + max(slot.battery_kw, 0.0)) * 1000
                limit = curtail_limit_w(
                    max_w=max_w,
                    pv_power_w=state_float(self.hass, self.config.get(CONF_PV_POWER_ENTITY)),
                    grid_power_w=state_float(self.hass, self.config.get(CONF_GRID_POWER_ENTITY)),
                    fallback_w=fallback,
                )
            await self._write_pv_limit(limit)
            if not self.curtailing:
                self.curtailing = True
                await self._async_save_store()
        elif self.curtailing:
            original = self._originals_obj().pv_limit_w
            await self._write_pv_limit(original if original is not None else max_w, force=True)
            self.curtailing = False
            await self._async_save_store()

    async def _write_pv_limit(self, limit_w: float, force: bool = False) -> None:
        entity_id = self.config[CONF_PV_LIMIT_ENTITY]
        unit = self.config[CONF_PV_LIMIT_UNIT]
        max_w = float(self.config[CONF_PV_LIMIT_MAX_W])
        current = state_float(self.hass, entity_id)
        if (
            current is not None
            and not force
            and abs(from_entity_value(current, unit, max_w) - limit_w) < PV_LIMIT_HYSTERESIS_W
        ):
            return
        value = to_entity_value(limit_w, unit, max_w)
        if current is not None and abs(current - value) < 1e-6:
            return
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
        )
