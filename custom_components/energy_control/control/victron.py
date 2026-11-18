"""Victron ESS controller: executes the plan via Home Assistant entities.

Writes only on change. On first takeover the original actuator values are
saved (persistently) and restored when the mode is set to off/advisory, the
entry is unloaded, or Home Assistant stops.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from math import isfinite
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
    CONF_EXPORT_ENERGY_ENTITY,
    CONF_FEED_IN_LIMIT_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_IDLE_SETPOINT_W,
    CONF_IMPORT_ENERGY_ENTITY,
    CONF_LOAD_ENTITY,
    CONF_MAX_CHARGE_W,
    CONF_MAX_DISCHARGE_W,
    CONF_MAX_EXPORT_W,
    CONF_MAX_IMPORT_W,
    CONF_PV_LIMIT_ENTITY,
    CONF_PV_LIMIT_MAX_W,
    CONF_PV_LIMIT_UNIT,
    CONF_PV_POWER_ENTITY,
    CONF_PV_PRODUCTION_ENTITIES,
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
from ..providers.telemetry import report_error, soc_reading
from ..providers.util import to_float
from .base import Desired, Limits, Originals, desired_for
from .pv_limiter import curtail_limit_w, from_entity_value, to_entity_value

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DESS_OFF_OPTIONS = ("off", "disabled", "0")
WRITE_TOLERANCE_W = 5.0
PV_LIMIT_HYSTERESIS_W = 40.0
REPLAN_SOC_DEVIATION = 5.0  # % points
REPLAN_MIN_INTERVAL_S = 60  # minimum plan age before a drift-triggered replan
STORE_DISPATCHED = "_ess_dispatched"
STORE_UNITS = "_units"
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
# Everything the saved originals depend on: if any of these change, a reload
# must restore the originals with the old configuration first.
HARDWARE_KEYS = (
    *TAKEOVER_KEYS,
    CONF_DESS_MODE_ENTITY,
    CONF_PV_LIMIT_ENTITY,
    CONF_PV_LIMIT_UNIT,
    CONF_PV_LIMIT_MAX_W,
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
        self._restore_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.pending_restores")
        self.pending_restores: dict[str, Any] = {}
        self._original_units: dict[str, str | None] = {}
        self._restore_attempt: datetime | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._unsub_stop: Callable[[], None] | None = None
        self._stopping = False
        self._waiting_logged = False
        self.active = False  # originals are saved; may still be awaiting DESS handoff
        self._ess_dispatched = False
        self.desired: Desired | None = None
        self.curtailing = False
        self.status = "idle"
        self.last_error: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._notified: tuple[Any, ...] | None = None
        self._lock = asyncio.Lock()
        # New configuration of a pending options reload. Control is handed over
        # to the next controller instead of restore + immediate re-takeover,
        # which could save our own forced values as "originals" when the
        # actuator states lag behind the writes.
        self.reload_config: dict[str, Any] | None = None
        self._dess_attempt: datetime | None = None
        self._guard_replan: datetime | None = None

    # --- lifecycle ---------------------------------------------------------
    @property
    def can_control(self) -> bool:
        """True when at least the grid setpoint entity is configured."""
        return bool(self.config.get(CONF_SETPOINT_ENTITY))

    @property
    def controlling(self) -> bool:
        """True after an ESS command was applied with a confirmed DESS handoff."""
        return self.active and self.desired is not None and self.status not in ("error", "restore_failed")

    async def async_load(self) -> None:
        """Load saved originals: present after a crash or a control-keeping reload."""
        self.pending_restores = await self._restore_store.async_load() or {}
        await self._retry_restores()
        stored = await self._store.async_load()
        if stored is not None:
            # We still own the hardware.
            stored = dict(stored)
            self.curtailing = bool(stored.pop(STORE_CURTAILING, False))
            self._original_units = stored.pop(STORE_UNITS, {})
            self._ess_dispatched = stored.pop(STORE_DISPATCHED, True)
            self._originals = stored
            self.active = True

    async def async_start(self) -> None:
        """Start the control loop (call async_load first)."""
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
        """Stop and hand control back (or over, on an options reload)."""
        self._stopping = True
        self._cancel_listeners()
        if self.reload_config is not None:
            async with self._lock:
                await self._async_hand_over(self.reload_config)
            return
        await self.async_release()

    async def _async_hand_over(self, new_config: dict[str, Any]) -> None:
        """Keep control across a reload; restore only what `new_config` drives differently.

        The remaining originals stay in the store for the next controller, which
        captures newly configured actuators before writing to them.
        """
        if not self.active or self._originals is None:
            return
        originals = self._originals
        changed = {key for key in HARDWARE_KEYS if self.config.get(key) != new_config.get(key)}
        errors: list[str] = []
        for key in TAKEOVER_KEYS:
            if key in changed and key in originals:
                await self._queue_restore(key, originals[key])
                try:
                    await self._write_number(key, float(originals[key]))
                except HomeAssistantError as err:
                    errors.append(f"{self.config.get(key)}={originals[key]}: {err}")
                    await self._queue_restore(key, originals[key])
                originals.pop(key)
        pv_keys = {CONF_PV_LIMIT_ENTITY, CONF_PV_LIMIT_UNIT, CONF_PV_LIMIT_MAX_W}
        if changed & pv_keys and self.config.get(CONF_PV_LIMIT_ENTITY):
            if CONF_PV_LIMIT_ENTITY in originals or self.curtailing:
                pv_original = originals.get(CONF_PV_LIMIT_ENTITY, self.config[CONF_PV_LIMIT_MAX_W])
                await self._queue_restore(CONF_PV_LIMIT_ENTITY, pv_original)
                try:
                    await self._write_pv_limit(float(pv_original), force=True)
                except HomeAssistantError as err:
                    errors.append(f"{self.config[CONF_PV_LIMIT_ENTITY]}={pv_original}: {err}")
                    await self._queue_restore(CONF_PV_LIMIT_ENTITY, pv_original)
            originals.pop(CONF_PV_LIMIT_ENTITY, None)
            self.curtailing = False
        dess = self.config.get(CONF_DESS_MODE_ENTITY)
        if CONF_DESS_MODE_ENTITY in changed and CONF_DESS_MODE_ENTITY in originals:
            if dess:
                await self._queue_restore(CONF_DESS_MODE_ENTITY, originals[CONF_DESS_MODE_ENTITY])
                try:
                    await self._select(dess, originals[CONF_DESS_MODE_ENTITY])
                except HomeAssistantError as err:
                    errors.append(f"{dess}={originals[CONF_DESS_MODE_ENTITY]}: {err}")
                    await self._queue_restore(CONF_DESS_MODE_ENTITY, originals[CONF_DESS_MODE_ENTITY])
            originals.pop(CONF_DESS_MODE_ENTITY)
        if errors:
            # Retries survive reloads and restarts in the separate recovery store.
            _LOGGER.error("Energy Control could not restore replaced actuators: %s", "; ".join(errors))
        await self._async_save_store()
        _LOGGER.debug("Energy Control hands ESS control over to the reloaded entry")

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
        # The control loop runs every few seconds: only wake entities and
        # dashboard subscriptions when something they show actually changed.
        snapshot = (
            self.status,
            self.active,
            self.curtailing,
            self.desired,
            self.last_error,
            self.coordinator.override,
            repr(self.pending_restores),
        )
        if snapshot == self._notified:
            return
        self._notified = snapshot
        for update in list(self._listeners):
            update()

    @callback
    def _on_update(self) -> None:
        if not self._stopping:
            self.hass.async_create_task(self.async_apply())

    async def _tick(self, now: datetime) -> None:
        self._record(now)
        await self.async_apply()
        if not self._stopping:
            # Heat pump and EV follow the plan independently of the ESS:
            # a failing battery actuator must not keep the heat pump blocked.
            try:
                await self.coordinator.flex.async_apply(now)
            except HomeAssistantError as err:
                _LOGGER.warning("Flexible load control failed: %s", err)

    # --- history -----------------------------------------------------------
    def _record(self, now: datetime) -> None:
        plan = self.coordinator.data
        planned = plan.slot_at(now) if plan else None
        soc = soc_reading(self.hass, self.config.get(CONF_SOC_ENTITY), now)[0]
        self.history.sample(
            now,
            mode=str(self.coordinator.mode),
            planned=planned,
            grid_w=self._power(CONF_GRID_POWER_ENTITY),
            battery_w=self._power(CONF_BATTERY_POWER_ENTITY),
            soc_pct=soc,
            curtailing=self.curtailing,
            load_w=self._load_power(),
            pv_w=self._pv_production(),
            import_kwh=self._energy(CONF_IMPORT_ENERGY_ENTITY),
            export_kwh=self._energy(CONF_EXPORT_ENERGY_ENTITY),
            meters_configured=bool(
                self.config.get(CONF_IMPORT_ENERGY_ENTITY) and self.config.get(CONF_EXPORT_ENERGY_ENTITY)
            ),
        )
        # Replan when reality drifts away from the plan. Not while planning
        # fails (the old plan is kept and would trigger a retry every tick) and
        # not right after a replan.
        if (
            planned is not None
            and soc is not None
            and self.coordinator.last_update_success
            and plan is not None
            and now - plan.created > timedelta(seconds=REPLAN_MIN_INTERVAL_S)
        ):
            frac = (now - planned.start).total_seconds() / (planned.hours * 3600 or 1)
            expected = planned.soc_start_pct + (planned.soc_end_pct - planned.soc_start_pct) * frac
            if abs(soc - expected) > REPLAN_SOC_DEVIATION:
                self.hass.async_create_task(self.coordinator.async_request_refresh())

    def _energy(self, key: str) -> float | None:
        entity_id = self.config.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        value = state_float(self.hass, entity_id)
        if state is None or value is None or value < 0 or report_error(state):
            return None
        unit = state.attributes.get("unit_of_measurement")
        return value if unit == "kWh" else value / 1000 if unit == "Wh" else None

    def _power(self, key: str) -> float | None:
        entity_id = self.config.get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.attributes.get("unit_of_measurement") not in ("W", "kW") or report_error(state):
            return None
        return state_float(self.hass, entity_id)

    def _load_power(self) -> float | None:
        return self._power(CONF_LOAD_ENTITY)

    def _pv_production(self) -> float | None:
        """Total PV power (W): the sum of all production sensors.

        Falls back to the curtailable inverter's sensor. A partial sum would
        look like clouds, so one missing sensor makes the total unknown.
        """
        entities = self.config.get(CONF_PV_PRODUCTION_ENTITIES) or []
        if not entities:
            return self._power(CONF_PV_POWER_ENTITY)
        total = 0.0
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            value = state_float(self.hass, entity_id)
            unit = state.attributes.get("unit_of_measurement") if state else None
            if state is None or value is None or unit not in ("W", "kW") or report_error(state):
                return None
            total += value  # state_float already converts kW to W
        return total

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

    def _propose(self, now: datetime) -> tuple[Action, SlotPlan | None, str]:
        """Action for `now`, the plan slot used (if any) and a status string."""
        coordinator = self.coordinator
        mode = coordinator.mode
        override = coordinator.override
        if override is not None:
            if now < override.until:
                return override.action, None, "override"
            coordinator.override = None

        manual = {
            Mode.SELF_CONSUMPTION: Action.SELF_CONSUME,
            Mode.HOLD: Action.HOLD,
            Mode.FORCE_CHARGE: Action.CHARGE_GRID,
            Mode.FORCE_DISCHARGE: Action.DISCHARGE_GRID,
        }
        if mode in manual:
            return manual[mode], None, "manual"
        plan = coordinator.data
        if plan is None or now - plan.created > timedelta(minutes=STALE_PLAN_MINUTES):
            return Action.HOLD, None, "fallback_stale_plan"
        slot = plan.slot_at(now)
        if slot is None:
            return Action.HOLD, None, "fallback_no_slot"
        if slot.price_estimated:
            # Published prices did not arrive: plain self consumption is the
            # safe default, never an arbitrage bet on a prediction.
            return Action.SELF_CONSUME, None, "fallback_estimated_price"
        if slot.grid_violation_kw > 1e-6:
            return slot.action, slot, "grid_constraint_violation"
        return slot.action, slot, "ok"

    def decide(self, now: datetime) -> tuple[Action, SlotPlan | None, str]:
        """Guard every proposed action with live telemetry and the SoC window."""
        action, slot, status = self._propose(now)
        soc, error = soc_reading(self.hass, self.config.get(CONF_SOC_ENTITY), now)
        if soc is None:
            return Action.HOLD, None, f"fallback_{error}"
        minimum = float(self.coordinator.settings[SET_MIN_SOC])
        maximum = float(self.coordinator.settings[SET_MAX_SOC])
        if soc <= minimum and (action == Action.DISCHARGE_GRID or (slot and slot.battery_kw < 0)):
            return Action.HOLD, None, "min_soc_reached"
        if soc >= maximum and (action == Action.CHARGE_GRID or (slot and slot.battery_kw > 0)):
            return Action.HOLD, None, "max_soc_reached"
        if action == Action.DISCHARGE_GRID:
            plan = self.coordinator.data
            current = plan.slot_at(now) if plan else None
            if current is None or not current.export_price_known:
                return Action.HOLD, None, "fallback_export_price_unknown"
        return action, slot, status

    # --- apply -------------------------------------------------------------
    async def async_apply(self) -> None:
        """Bring the hardware in line with the current decision."""
        async with self._lock:
            await self._async_apply()

    async def _async_apply(self) -> None:
        if self._stopping:
            return
        await self._retry_restores()
        mode = self.coordinator.mode
        if mode not in CONTROL_MODES or not self.can_control:
            if self.active:
                await self._async_release()
            if not self.active:
                self.status = "advisory" if mode == Mode.ADVISORY or not self.can_control else "off"
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

        try:
            self._validate_capabilities()
            if not await self._async_take_over():
                self.status = "waiting_for_entities"
                self.desired = None
                self._notify()
                return
            await self._set_dess_off()
            desired = desired_for(
                action,
                battery_kw=battery_kw,
                grid_kw=grid_kw,
                curtail=curtail,
                originals=self._originals_obj(),
                limits=limits,
            )
            soc, _error = soc_reading(self.hass, self.config.get(CONF_SOC_ENTITY), now)
            minimum = float(self.coordinator.settings[SET_MIN_SOC])
            maximum = float(self.coordinator.settings[SET_MAX_SOC])
            # Block the prohibited direction even in ordinary self consumption.
            desired = replace(
                desired,
                charge_limit=0.0 if soc is None or soc >= maximum else desired.charge_limit,
                discharge_limit=0.0 if soc is None or soc <= minimum else desired.discharge_limit,
                feed_in_limit=min(desired.feed_in_limit, limits.max_export)
                if desired.feed_in_limit >= 0
                else limits.max_export,
            )
            if (status.startswith("fallback_") or status.endswith("soc_reached")) and (
                self._guard_replan is None or now - self._guard_replan >= timedelta(seconds=60)
            ):
                self._guard_replan = now
                self.hass.async_create_task(self.coordinator.async_request_refresh())
            originals = self._originals or {}
            targets = (
                (CONF_SETPOINT_ENTITY, desired.setpoint),
                (CONF_CHARGE_LIMIT_ENTITY, desired.charge_limit),
                (CONF_DISCHARGE_LIMIT_ENTITY, desired.discharge_limit),
                (CONF_FEED_IN_LIMIT_ENTITY, desired.feed_in_limit),
            )
            # Validate the complete command before the first write. Limits go
            # before the setpoint so a previous high limit cannot cause a surge.
            for key, value in targets:
                if key in originals:
                    self._validate_target(key, value)
            if not self._ess_dispatched:
                self._ess_dispatched = True
                await self._async_save_store()
            for key, value in (*targets[1:], targets[0]):
                # Never write an actuator whose original is not saved yet
                # (offline since a reload added it): it could not be restored.
                if key in originals:
                    await self._write_number(key, value)
            await self._apply_pv_limit(desired, slot)
            self.desired = desired
            self.status = status
            self.last_error = None
            ir.async_delete_issue(self.hass, DOMAIN, "control_failed")
        except HomeAssistantError as err:
            self.status = "error"
            if self._ess_dispatched:
                # Stop an earlier forced command after loss of an essential
                # capability or DESS ownership. Each available actuator is tried.
                for key, value in (
                    (CONF_CHARGE_LIMIT_ENTITY, 0.0),
                    (CONF_DISCHARGE_LIMIT_ENTITY, 0.0),
                    (CONF_SETPOINT_ENTITY, limits.idle_setpoint),
                ):
                    if key in (self._originals or {}):
                        with suppress(HomeAssistantError):
                            await self._write_number(key, value)
            self.desired = None
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

    def _validate_capabilities(self) -> None:
        """All ESS actions need directional limits and a feed-in cap.

        Requiring this common capability set also guarantees that the live SoC
        guard can block either direction at any time. Incomplete setups remain
        advisory, with the missing configuration explained in Repairs.
        """
        if any(self.config.get(key) in self.pending_restores for key in HARDWARE_KEYS):
            raise HomeAssistantError("Actuator restore pending; resolve the Repairs issue before control")
        missing = [key for key in TAKEOVER_KEYS if not self.config.get(key)]
        if missing:
            raise HomeAssistantError(f"Advisory only; configure ESS actuators: {', '.join(missing)}")
        for key in TAKEOVER_KEYS:
            entity = self.config[key]
            state = self.hass.states.get(entity)
            if state is None or state_float(self.hass, entity) is None:
                if self.active:
                    raise HomeAssistantError(f"Essential actuator unavailable: {entity}")
                continue
            if state.attributes.get("unit_of_measurement") not in (None, "W", "kW"):
                raise HomeAssistantError(f"Unsupported power unit: {entity}")

    def _validate_target(self, key: str, value: float) -> None:
        state = self.hass.states.get(self.config[key])
        if state is None or state_float(self.hass, self.config[key]) is None or not isfinite(value):
            raise HomeAssistantError(f"Unavailable actuator or invalid target: {self.config[key]}")
        factor = 1000 if state.attributes.get("unit_of_measurement") == "kW" else 1
        for attr, lower in (("min", True), ("max", False)):
            if attr in state.attributes:
                bound = to_float(state.attributes[attr])
                if bound is None or (value / factor < bound if lower else value / factor > bound):
                    raise HomeAssistantError(f"Target {value} W outside {attr} for {self.config[key]}")

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
            for key in (*TAKEOVER_KEYS, CONF_PV_LIMIT_ENTITY):
                entity_id = self.config.get(key)
                state = self.hass.states.get(entity_id) if entity_id else None
                if state is not None and key in self._originals and entity_id not in self._original_units:
                    self._original_units[entity_id] = state.attributes.get("unit_of_measurement")
            await self._store.async_save(
                {
                    **self._originals,
                    STORE_CURTAILING: self.curtailing,
                    STORE_UNITS: self._original_units,
                    STORE_DISPATCHED: self._ess_dispatched,
                }
            )

    async def _async_take_over(self) -> bool:
        """Save the original settings and take control. False while not possible yet."""
        if self.active and self._originals is not None:
            await self._async_capture_late()
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
        return True

    async def _async_capture_late(self) -> None:
        """Save originals of actuators that were offline or newly configured."""
        originals = self._originals
        assert originals is not None
        captured: dict[str, Any] = {}
        for key in TAKEOVER_KEYS:
            if (
                self.config.get(key)
                and key not in originals
                and (value := state_float(self.hass, self.config.get(key))) is not None
            ):
                captured[key] = value
        # Not while curtailing: the limiter then holds our value, not the original.
        if (
            self.config.get(CONF_PV_LIMIT_ENTITY)
            and CONF_PV_LIMIT_ENTITY not in originals
            and not self.curtailing
            and (pv_original := self._pv_limit_original()) is not None
        ):
            captured[CONF_PV_LIMIT_ENTITY] = pv_original
        dess = self.config.get(CONF_DESS_MODE_ENTITY)
        if (
            dess
            and CONF_DESS_MODE_ENTITY not in originals
            and (state := self.hass.states.get(dess)) is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ):
            captured[CONF_DESS_MODE_ENTITY] = state.state
        if not captured:
            return
        originals.update(captured)
        await self._async_save_store()
        _LOGGER.info("Energy Control saved originals of %s", ", ".join(captured))

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
            self.status = "restore_failed"
            self.last_error = "; ".join(errors)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "control_failed",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="control_failed",
                translation_placeholders={"error": self.last_error},
            )
            return
        self.active = False
        self._ess_dispatched = False
        self.curtailing = False
        self.desired = None
        self.last_error = None
        ir.async_delete_issue(self.hass, DOMAIN, "control_failed")
        self._originals = None
        await self._store.async_remove()
        _LOGGER.info("Energy Control released ESS control")

    async def _queue_restore(self, key: str, value: Any) -> None:
        entity_id = self.config[key]
        state = self.hass.states.get(entity_id)
        self.pending_restores[entity_id] = {
            "value": value,
            "kind": "select" if key == CONF_DESS_MODE_ENTITY else "number",
            "unit": self._original_units.get(entity_id, state.attributes.get("unit_of_measurement") if state else None),
            "pv_unit": self.config[CONF_PV_LIMIT_UNIT] if key == CONF_PV_LIMIT_ENTITY else None,
            "pv_max_w": self.config[CONF_PV_LIMIT_MAX_W] if key == CONF_PV_LIMIT_ENTITY else None,
        }
        await self._restore_store.async_save(self.pending_restores)
        self._restore_issue()

    def _restore_issue(self) -> None:
        issue_id = f"pending_restores_{self.coordinator.config_entry.entry_id}"
        if not self.pending_restores:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="pending_restores",
            translation_placeholders={"restores": str(self.pending_restores)},
        )

    async def _retry_restores(self) -> None:
        now = dt_util.utcnow()
        if self._restore_attempt and now - self._restore_attempt < timedelta(seconds=30):
            return
        self._restore_attempt = now
        for entity_id, restore in list(self.pending_restores.items()):
            try:
                state = self.hass.states.get(entity_id)
                if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                    continue
                value = restore["value"]
                if restore["kind"] == "select":
                    if state.state != value:
                        await self._select(entity_id, value)
                        continue  # require readback before clearing persistent recovery
                else:
                    if restore["pv_unit"]:
                        value = to_entity_value(value, restore["pv_unit"], restore["pv_max_w"])
                    elif restore["unit"] == "kW":
                        value /= 1000
                    if state.attributes.get("unit_of_measurement") != restore["unit"]:
                        continue  # changed units require human reconciliation
                    if to_float(state.state) is None or abs(float(state.state) - value) > 1e-6:
                        await self.hass.services.async_call(
                            "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
                        )
                        continue
                self.pending_restores.pop(entity_id)
            except HomeAssistantError as err:
                _LOGGER.warning("Pending restore of %s failed: %s", entity_id, err)
        if self.pending_restores:
            await self._restore_store.async_save(self.pending_restores)
        else:
            await self._restore_store.async_remove()
        self._restore_issue()

    # --- writers -----------------------------------------------------------
    async def _set_dess_off(self) -> None:
        dess = self.config.get(CONF_DESS_MODE_ENTITY)
        if not dess:
            return
        state = self.hass.states.get(dess)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise HomeAssistantError(f"DESS unavailable: {dess}")
        options = state.attributes.get("options") or []
        target = next((o for o in options if str(o).lower() in DESS_OFF_OPTIONS), None)
        if target is None:
            raise HomeAssistantError(f"No supported DESS off option: {dess} ({options})")
        if state.state == target:
            return
        now = dt_util.utcnow()
        if self._dess_attempt is None or now - self._dess_attempt >= timedelta(seconds=30):
            self._dess_attempt = now
            await self._select(dess, target)
        # MQTT acknowledgement can arrive later. Do not block the event loop
        # waiting for it: the next tick checks readback, retrying at most /30s.
        readback = self.hass.states.get(dess)
        if readback is None or readback.state != target:
            raise HomeAssistantError(f"Waiting for DESS off acknowledgement: {dess}")

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
        self._validate_target(key, value * factor)
        current = state_float(self.hass, entity_id)
        target_w = value * factor
        if current is not None and (
            current == target_w or (current > 0 and target_w > 0 and abs(current - target_w) <= WRITE_TOLERANCE_W)
        ):
            return
        _LOGGER.debug("Set %s: %s -> %s", entity_id, current, value)
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
        )

    async def _apply_pv_limit(self, desired: Desired, slot: SlotPlan | None) -> None:
        if not self.config.get(CONF_PV_LIMIT_ENTITY):
            # Without a PV limiter only the feed-in limit can curtail.
            self.curtailing = desired.curtail and bool(self.config.get(CONF_FEED_IN_LIMIT_ENTITY))
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
