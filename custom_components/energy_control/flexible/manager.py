"""Heat pump and EV: models, plan integration and execution."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_EV_CAPACITY_KWH,
    CONF_EV_CONNECTED_ENTITY,
    CONF_EV_POWER_ENTITY,
    CONF_EV_POWER_W,
    CONF_EV_SOC_ENTITY,
    CONF_EV_SWITCH_ENTITY,
    CONF_HP_BLOCK_FULL_TEMP,
    CONF_HP_BLOCK_MIN_TEMP,
    CONF_HP_BOOST,
    CONF_HP_COMFORT,
    CONF_HP_CONTROL_ENTITY,
    CONF_HP_MAX_BLOCK_DAY_H,
    CONF_HP_MAX_BLOCK_MIN,
    CONF_HP_SWITCH_ON_BLOCKS,
    CONF_INDOOR_MIN_TEMP,
    CONF_INDOOR_TEMP_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    DOMAIN,
    SET_EV_ENERGY,
    SET_EV_MODE,
    SET_EV_READY_BY,
    SET_EV_TARGET_SOC,
    SET_HP_CONTROL,
    Mode,
)
from ..optimizer.model import Plan, SlotInput
from ..providers.price_forecast import hour_key, linear_fit
from ..providers.slots import floor_slot
from ..providers.util import to_float
from .ev import EvMode, deadline, ev_energy, schedule_ev
from .heatpump import HeatPumpParams, HpMode, HpSlot, load_with_heat_pump, max_block, schedule_heat_pump

if TYPE_CHECKING:
    from ..coordinator import EnergyControlCoordinator

_LOGGER = logging.getLogger(__name__)

HEATING_BASE_C = 18.0  # heating demand ~ degrees below this
EV_CHARGE_EFFICIENCY = 0.9
MODE_KEYWORDS = {
    HpMode.BLOCK: ("evu", "block", "sperr", "lock", "off"),
    HpMode.NORMAL: ("normal",),
    HpMode.COMFORT: ("comfort", "recommend", "increase"),
    HpMode.BOOST: ("boost", "force"),
}


def sg_ready_options(options: list[str]) -> dict[HpMode, str]:
    """Map SG Ready states to a select's options by keyword."""
    found: dict[HpMode, str] = {}
    for mode, words in MODE_KEYWORDS.items():
        for option in options:
            if any(word in option.lower() for word in words) and option not in found.values():
                found[mode] = option
                break
    return found


def _state_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return to_float(state.state)


class FlexibleLoads:
    """Plans and drives the heat pump (SG Ready / EVU) and EV charger."""

    def __init__(self, hass: HomeAssistant, coordinator: EnergyControlCoordinator) -> None:
        """Init."""
        self.hass = hass
        self.coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{coordinator.config_entry.entry_id}.flexible")
        # Heat pump model: power (kW) = a + b * max(0, 18 - T_outdoor)
        self.hp_model: tuple[float, float] | None = None
        self.hp_modes: dict[datetime, HpMode] = {}
        self.ev_slots: set[datetime] = set()
        self.hp_mode: HpMode | None = None  # what we last commanded
        self.hp_status = "off"
        self.ev_status = "off"
        self.blocked_since: datetime | None = None
        self.recovery_until: datetime | None = None
        self.blocks: list[tuple[datetime, datetime]] = []  # finished blocks (daily budget)
        self.hp_active = False
        self.ev_active = False
        self._ev_on: bool | None = None
        self._ev_delivered = 0.0
        self._last_tick: datetime | None = None
        self._conflict_logged = False

    @property
    def config(self) -> dict[str, Any]:
        """Configuration (shared with the coordinator)."""
        return self.coordinator.config

    @property
    def settings(self) -> dict[str, Any]:
        """Runtime settings."""
        return self.coordinator.settings

    @property
    def hp_configured(self) -> bool:
        """A control entity is configured."""
        return bool(self.config.get(CONF_HP_CONTROL_ENTITY))

    @property
    def ev_configured(self) -> bool:
        """An EV charger switch is configured."""
        return bool(self.config.get(CONF_EV_SWITCH_ENTITY))

    def params(self) -> HeatPumpParams:
        """Comfort limits from config."""
        c = self.config
        return HeatPumpParams(
            max_block_minutes=float(c[CONF_HP_MAX_BLOCK_MIN]),
            block_min_temp=float(c[CONF_HP_BLOCK_MIN_TEMP]),
            block_full_temp=float(c[CONF_HP_BLOCK_FULL_TEMP]),
            max_block_hours_per_day=float(c[CONF_HP_MAX_BLOCK_DAY_H]),
            comfort=bool(c[CONF_HP_COMFORT]),
            boost=bool(c[CONF_HP_BOOST]),
        )

    # --- lifecycle -----------------------------------------------------------
    async def async_load(self) -> None:
        """Restore whether we left the heat pump or charger in a commanded state."""
        data = await self._store.async_load() or {}
        self.hp_active = bool(data.get("hp_active"))
        self.ev_active = bool(data.get("ev_active"))
        if since := data.get("blocked_since"):
            self.blocked_since = dt_util.parse_datetime(since)

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "hp_active": self.hp_active,
                "ev_active": self.ev_active,
                "blocked_since": self.blocked_since.isoformat() if self.blocked_since else None,
            }
        )

    # --- models --------------------------------------------------------------
    def fit_hp_model(self, hp_kw: list[tuple[datetime, float]], temps: dict[datetime, float]) -> None:
        """Fit heat pump power against heating degrees from hourly history."""
        pairs = [
            (max(0.0, HEATING_BASE_C - temps[hour_key(when)]), kw)
            for when, kw in hp_kw
            if hour_key(when) in temps and kw >= 0
        ]
        fit = linear_fit(pairs)
        if fit is not None and fit[1] >= 0:
            self.hp_model = (max(0.0, fit[0]), fit[1])
        elif hp_kw:
            mean = sum(kw for _, kw in hp_kw) / len(hp_kw)
            self.hp_model = (max(0.0, mean), 0.0)
        _LOGGER.debug("Heat pump model from %d hours: %s", len(pairs), self.hp_model)

    def hp_power(self, temp: float | None) -> float:
        """Expected heat pump power (kW) in normal operation."""
        if self.hp_model is None:
            return 0.0
        a, b = self.hp_model
        if temp is None:
            return a
        return max(0.0, a + b * max(0.0, HEATING_BASE_C - temp))

    def temperature_at(self, when: datetime) -> float | None:
        """Forecast outdoor temperature, else the live reading."""
        forecast = self.coordinator.price_archive.temperature.get(hour_key(when))
        if forecast is not None:
            return forecast
        return _state_float(self.hass, self.config.get(CONF_OUTDOOR_TEMP_ENTITY))

    # --- planning ------------------------------------------------------------
    def with_expected_load(self, slots: list[SlotInput]) -> list[SlotInput]:
        """Add the heat pump's normal consumption to a base-load forecast."""
        if not self.hp_configured or self.hp_model is None:
            return slots
        out = []
        for s in slots:
            kwh = self.hp_power(self.temperature_at(s.start)) * s.hours
            out.append(
                replace(
                    s,
                    load_kwh=s.load_kwh + kwh,
                    load_forecast_kwh=(s.load_forecast_kwh or 0.0) + kwh,
                )
            )
        return out

    def schedule(self, slots: list[SlotInput], plan: Plan, now: datetime) -> list[SlotInput] | None:
        """Schedule heat pump and EV on a first plan; slots with their load, or None."""
        changed = False
        delta = [0.0] * len(slots)
        self.hp_modes = {}
        self.ev_slots = set()
        if self.hp_configured and self.hp_model is not None and self._hp_enabled():
            hp_slots = [
                HpSlot(p, self.hp_power(self.temperature_at(p.start)), self.temperature_at(p.start)) for p in plan.slots
            ]
            params = self.params()
            modes = schedule_heat_pump(
                hp_slots,
                params,
                now=now,
                blocked_since=self.blocked_since,
                blocked_today_h=self.blocked_today_h(now),
                recovery_until=self.recovery_until,
            )
            self.hp_modes = {p.start: m for p, m in zip(plan.slots, modes, strict=True)}
            energy = load_with_heat_pump(hp_slots, modes, params)
            for i, s in enumerate(hp_slots):
                delta[i] += energy[i] - s.power_kw * s.plan.hours
            changed = any(m != HpMode.NORMAL for m in modes)
        if self.ev_configured and (need := self.ev_need_kwh()) > 0 and self._ev_plugged():
            mode = self.ev_mode
            power = float(self.config[CONF_EV_POWER_W]) / 1000
            if mode == EvMode.SMART:
                ready = deadline(now, self.ev_ready_by, dt_util.get_default_time_zone())
                self.ev_slots = schedule_ev(plan.slots, need, power, now=now, ready_by=ready)
            elif mode == EvMode.NOW:
                hours = need / power
                self.ev_slots = {p.start for p in plan.slots if p.start < now + timedelta(hours=hours)}
            for i, kwh in enumerate(ev_energy(plan.slots, self.ev_slots, power)):
                delta[i] += kwh
            changed = changed or bool(self.ev_slots)
        if not changed:
            return None
        return [
            replace(s, load_kwh=max(0.0, s.load_kwh + d), load_forecast_kwh=max(0.0, (s.load_forecast_kwh or 0.0) + d))
            for s, d in zip(slots, delta, strict=True)
        ]

    def annotate(self, plan: Plan) -> None:
        """Attach the heat pump mode and EV power to the plan slots (dashboard)."""
        power = float(self.config.get(CONF_EV_POWER_W) or 0) / 1000
        for slot in plan.slots:
            mode = self.hp_modes.get(slot.start)
            slot.hp_mode = str(mode) if mode is not None else None
            slot.ev_kw = power if slot.start in self.ev_slots else 0.0

    # --- heat pump state -------------------------------------------------------
    def _hp_enabled(self) -> bool:
        return bool(self.settings.get(SET_HP_CONTROL)) and self.coordinator.mode not in (Mode.OFF, Mode.ADVISORY)

    def blocked_today_h(self, now: datetime) -> float:
        """Hours blocked since local midnight (finished and running)."""
        midnight = dt_util.start_of_local_day(now)
        total = sum(
            (min(end, now) - max(start, midnight)).total_seconds() for start, end in self.blocks if end > midnight
        )
        if self.blocked_since is not None:
            total += (now - max(self.blocked_since, midnight)).total_seconds()
        return total / 3600

    def desired_hp_mode(self, now: datetime) -> tuple[HpMode, str]:
        """Planned mode for now, after the live safety guards."""
        mode = self.hp_modes.get(floor_slot(now), HpMode.NORMAL)
        params = self.params()
        if mode != HpMode.BLOCK:
            return mode, "planned" if mode != HpMode.NORMAL else "normal"
        outdoor = _state_float(self.hass, self.config.get(CONF_OUTDOOR_TEMP_ENTITY))
        if outdoor is None and self.config.get(CONF_OUTDOOR_TEMP_ENTITY):
            return HpMode.NORMAL, "outdoor_temperature_unknown"
        temp = outdoor if outdoor is not None else self.temperature_at(now)
        limit = max_block(temp, params)
        if limit <= timedelta(0):
            return HpMode.NORMAL, "too_cold_to_block"
        indoor = _state_float(self.hass, self.config.get(CONF_INDOOR_TEMP_ENTITY))
        if indoor is not None and indoor < float(self.config[CONF_INDOOR_MIN_TEMP]):
            return HpMode.NORMAL, "house_too_cold"
        if self.recovery_until is not None and now < self.recovery_until:
            return HpMode.NORMAL, "recovering"
        if self.blocked_since is not None and now - self.blocked_since >= limit:
            return HpMode.NORMAL, "block_limit_reached"
        budget = params.max_block_hours_per_day * (limit / timedelta(minutes=params.max_block_minutes))
        if self.blocked_since is None and self.blocked_today_h(now) >= budget:
            return HpMode.NORMAL, "daily_block_budget_used"
        return HpMode.BLOCK, "blocked"

    def _option_for(self, mode: HpMode, options: list[str]) -> str | None:
        mapping = sg_ready_options(options)
        for candidate in (mode, HpMode.COMFORT if mode == HpMode.BOOST else HpMode.NORMAL, HpMode.NORMAL):
            if candidate in mapping:
                return mapping[candidate]
        return None

    async def _async_write_hp(self, mode: HpMode) -> None:
        entity_id = self.config[CONF_HP_CONTROL_ENTITY]
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise HomeAssistantError(f"{entity_id} unavailable")
        if entity_id.startswith("select."):
            option = self._option_for(mode, list(state.attributes.get("options") or []))
            if option is None:
                raise HomeAssistantError(f"No SG Ready options recognised on {entity_id}")
            if state.state != option:
                await self.hass.services.async_call(
                    "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True
                )
            return
        blocks_on = bool(self.config[CONF_HP_SWITCH_ON_BLOCKS])
        turn_on = (mode == HpMode.BLOCK) == blocks_on
        if (state.state == STATE_ON) != turn_on:
            domain = entity_id.split(".", 1)[0]
            await self.hass.services.async_call(
                domain, "turn_on" if turn_on else "turn_off", {"entity_id": entity_id}, blocking=True
            )

    async def _async_apply_hp(self, now: datetime) -> None:
        if not self.hp_configured:
            return
        if not self._hp_enabled():
            if self.hp_active:
                await self._async_release_hp(now)
            self.hp_status = "off"
            return
        mode, status = self.desired_hp_mode(now)
        try:
            await self._async_write_hp(mode)
        except HomeAssistantError as err:
            self.hp_status = "unavailable"
            _LOGGER.debug("Heat pump control: %s", err)
            return
        if self.hp_mode is not None and mode == self.hp_mode:
            self._check_conflict(mode)
        if mode == HpMode.BLOCK and self.blocked_since is None:
            self.blocked_since = now
        elif mode != HpMode.BLOCK and self.blocked_since is not None:
            self._finish_block(now)
        changed = not self.hp_active
        self.hp_active = True
        self.hp_mode = mode
        self.hp_status = status
        if changed or mode == HpMode.BLOCK:
            await self._async_save()

    def _finish_block(self, now: datetime) -> None:
        assert self.blocked_since is not None
        self.blocks.append((self.blocked_since, now))
        self.recovery_until = now + max(now - self.blocked_since, timedelta(minutes=30))
        self.blocks = [b for b in self.blocks if b[1] > now - timedelta(days=1)]
        self.blocked_since = None

    def _check_conflict(self, mode: HpMode) -> None:
        """Warn once when something else keeps changing the heat pump mode."""
        entity_id = self.config[CONF_HP_CONTROL_ENTITY]
        state = self.hass.states.get(entity_id)
        if state is None or not entity_id.startswith("select.") or self._conflict_logged:
            return
        option = self._option_for(mode, list(state.attributes.get("options") or []))
        if option is not None and state.state != option:
            self._conflict_logged = True
            _LOGGER.warning(
                "%s changed to %s by something else while Energy Control expects %s; "
                "disable other automations that set the heat pump mode",
                entity_id,
                state.state,
                option,
            )

    async def _async_release_hp(self, now: datetime) -> None:
        """Hand the heat pump back in normal operation (never leave it blocked)."""
        try:
            await self._async_write_hp(HpMode.NORMAL)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not return the heat pump to normal: %s", err)
            return
        if self.blocked_since is not None:
            self._finish_block(now)
        self.hp_active = False
        self.hp_mode = None
        await self._async_save()

    # --- EV --------------------------------------------------------------------
    @property
    def ev_mode(self) -> EvMode:
        """Charging mode."""
        try:
            return EvMode(self.settings.get(SET_EV_MODE, EvMode.SMART))
        except ValueError:
            return EvMode.MANUAL

    @property
    def ev_ready_by(self) -> time:
        """Ready-by time."""
        value = self.settings.get(SET_EV_READY_BY, "07:00:00")
        try:
            return time.fromisoformat(str(value))
        except ValueError:
            return time(7, 0)

    def _ev_plugged(self) -> bool:
        entity_id = self.config.get(CONF_EV_CONNECTED_ENTITY)
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == STATE_ON

    def ev_need_kwh(self) -> float:
        """Energy still to charge (kWh, at the wall)."""
        soc = _state_float(self.hass, self.config.get(CONF_EV_SOC_ENTITY))
        capacity = float(self.config.get(CONF_EV_CAPACITY_KWH) or 0)
        if soc is not None and capacity > 0:
            target = float(self.settings.get(SET_EV_TARGET_SOC, 80))
            return max(0.0, (target - soc) / 100 * capacity / EV_CHARGE_EFFICIENCY)
        return max(0.0, float(self.settings.get(SET_EV_ENERGY, 0.0)))

    async def _async_apply_ev(self, now: datetime) -> None:
        if not self.ev_configured:
            return
        mode = self.ev_mode
        if mode == EvMode.MANUAL or self.coordinator.mode in (Mode.OFF, Mode.ADVISORY):
            if self.ev_active:
                await self._async_switch_ev(False)
                self.ev_active = False
                await self._async_save()
            self.ev_status = "manual"
            return
        need = self.ev_need_kwh()
        charge = self._ev_plugged() and need > 0 and floor_slot(now) in self.ev_slots
        await self._count_delivered(now)
        try:
            await self._async_switch_ev(charge)
        except HomeAssistantError as err:
            self.ev_status = "unavailable"
            _LOGGER.debug("EV charger control: %s", err)
            return
        if not self.ev_active:
            self.ev_active = True
            await self._async_save()
        self.ev_status = "charging" if charge else ("waiting" if need > 0 else "done")

    async def _count_delivered(self, now: datetime) -> None:
        """Count down the manual 'energy to charge' while charging."""
        last, self._last_tick = self._last_tick, now
        if not self._ev_on or last is None or self.config.get(CONF_EV_SOC_ENTITY):
            return
        seconds = min(120.0, (now - last).total_seconds())
        measured = _state_float(self.hass, self.config.get(CONF_EV_POWER_ENTITY))
        power_kw = measured / 1000 if measured is not None else float(self.config[CONF_EV_POWER_W]) / 1000
        self._ev_delivered += max(0.0, power_kw) * seconds / 3600
        if self._ev_delivered >= 0.1:
            remaining = max(0.0, float(self.settings.get(SET_EV_ENERGY, 0.0)) - self._ev_delivered)
            self._ev_delivered = 0.0
            self.settings[SET_EV_ENERGY] = round(remaining, 2)
            await self.coordinator.async_save_settings()

    async def _async_switch_ev(self, on: bool) -> None:
        entity_id = self.config[CONF_EV_SWITCH_ENTITY]
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise HomeAssistantError(f"{entity_id} unavailable")
        if (state.state == STATE_ON) != on:
            domain = entity_id.split(".", 1)[0]
            await self.hass.services.async_call(
                domain, "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True
            )
        self._ev_on = on

    # --- entry points ------------------------------------------------------------
    async def async_apply(self, now: datetime | None = None) -> None:
        """Bring heat pump and charger in line with the plan (controller tick)."""
        now = now or dt_util.utcnow()
        await self._async_apply_hp(now)
        await self._async_apply_ev(now)

    async def async_release(self) -> None:
        """Unload: heat pump back to normal, charger left as the user set it last."""
        now = dt_util.utcnow()
        if self.hp_configured and self.hp_active:
            await self._async_release_hp(now)
