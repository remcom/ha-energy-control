"""Coordinator: gathers inputs, runs the optimizer, owns runtime settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFF,
    CONF_DISCHARGE_EFF,
    CONF_ENERGY_TAX,
    CONF_EXPORT_FACTOR,
    CONF_EXPORT_FEE,
    CONF_EXPORT_FIXED,
    CONF_EXPORT_MODE,
    CONF_LOAD_DAYS,
    CONF_LOAD_ENTITY,
    CONF_LOAD_FALLBACK_W,
    CONF_MARKUP,
    CONF_MAX_CHARGE_W,
    CONF_MAX_DISCHARGE_W,
    CONF_MAX_EXPORT_W,
    CONF_MAX_IMPORT_W,
    CONF_NETTING_END,
    CONF_PRICE_ENTITY,
    CONF_PRICE_INCLUDES_TAXES,
    CONF_PV_ENTITIES,
    CONF_PV_ESTIMATE,
    CONF_PV_SCALE,
    CONF_SOC_ENTITY,
    CONF_VAT,
    DEFAULT_SETTINGS,
    DEFAULTS,
    DOMAIN,
    HORIZON_HOURS,
    LOAD_PROFILE_REFRESH_H,
    SET_ARBITRAGE_MARGIN,
    SET_BATTERY_TO_GRID,
    SET_CURTAIL_NEGATIVE,
    SET_CYCLE_COST,
    SET_GRID_CHARGE,
    SET_MAX_SOC,
    SET_MIN_SOC,
    SET_MODE,
    Mode,
)
from .optimizer import Action, BatteryParams, GridParams, Plan, Strategy, optimize
from .providers.load import LoadProfile, build_load_profile
from .providers.prices import ExportMode, PriceModel, PricePoint, parse_price_attributes
from .providers.slots import build_slots
from .providers.solar import EnergyInterval, parse_pv_attributes
from .providers.telemetry import soc_reading
from .providers.util import to_float

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Override:
    """Temporary manual action."""

    action: Action
    until: datetime
    power_w: float | None = None


def state_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Numeric state of an entity, or None."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, ""):
        return None
    value = to_float(state.state)
    if value is None:
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "")
    if unit == "kW":
        value *= 1000
    return value


class EnergyControlCoordinator(DataUpdateCoordinator[Plan]):
    """Plans the battery schedule every slot and on input changes."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Init."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
            request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=10, immediate=True),
        )
        # The options flow stores the complete configuration, so options replace
        # data (merging would resurrect fields cleared in the options flow).
        self.config: dict[str, Any] = {**DEFAULTS, **(entry.options or entry.data)}
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._settings_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.settings")
        self.load_profile: LoadProfile | None = None
        self._load_profile_at: datetime | None = None
        self.override: Override | None = None
        self.prices: list[PricePoint] = []
        self.pv: list[EnergyInterval] = []
        self.soc_pct: float | None = None
        self.last_plan_error: str | None = None

    # --- lifecycle ---------------------------------------------------------
    async def async_setup(self) -> None:
        """Load persisted settings and start listeners."""
        stored = await self._settings_store.async_load()
        if stored:
            self.settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})

        # Replan just after each quarter-hour boundary.
        self.config_entry.async_on_unload(
            async_track_time_change(self.hass, self._on_slot_boundary, minute=[0, 15, 30, 45], second=3)
        )
        watched = [self.config[CONF_PRICE_ENTITY], *self.config.get(CONF_PV_ENTITIES, [])]
        self.config_entry.async_on_unload(async_track_state_change_event(self.hass, watched, self._on_input_changed))

    @callback
    def _on_slot_boundary(self, _now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _on_input_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        old = event.data["old_state"]
        if new is None or (old is not None and old.state == new.state and old.attributes == new.attributes):
            return
        self.hass.async_create_task(self.async_request_refresh())

    # --- settings ----------------------------------------------------------
    @property
    def current_soc(self) -> float | None:
        """Live SoC (%), falling back to the value used for the last plan."""
        return soc_reading(self.hass, self.config.get(CONF_SOC_ENTITY))[0]

    @property
    def mode(self) -> Mode:
        """Current mode."""
        try:
            return Mode(self.settings[SET_MODE])
        except ValueError:
            return Mode.ADVISORY

    async def async_set_setting(self, key: str, value: Any) -> None:
        """Change a runtime setting, persist it and replan."""
        self.settings[key] = value
        await self._settings_store.async_save(self.settings)
        self.async_update_listeners()
        await self.async_request_refresh()

    # --- model inputs ------------------------------------------------------
    def price_model(self) -> PriceModel:
        """Price model from config."""
        c = self.config
        netting_end = c.get(CONF_NETTING_END)
        if isinstance(netting_end, str) and netting_end:
            netting_end = date.fromisoformat(netting_end)
        return PriceModel(
            includes_taxes=bool(c[CONF_PRICE_INCLUDES_TAXES]),
            markup=float(c[CONF_MARKUP]),
            energy_tax=float(c[CONF_ENERGY_TAX]),
            vat=float(c[CONF_VAT]) / 100,
            export_mode=ExportMode(c[CONF_EXPORT_MODE]),
            netting_end=netting_end or None,
            export_factor=float(c[CONF_EXPORT_FACTOR]),
            export_fee=float(c[CONF_EXPORT_FEE]),
            export_fixed=float(c[CONF_EXPORT_FIXED]),
        )

    def battery_params(self, soc_pct: float) -> BatteryParams:
        """Battery parameters from config + settings."""
        c, s = self.config, self.settings
        capacity = float(c[CONF_CAPACITY_KWH])
        min_soc = float(s[SET_MIN_SOC])
        max_soc = max(float(s[SET_MAX_SOC]), min_soc)
        return BatteryParams(
            capacity_kwh=capacity,
            soc_kwh=capacity * max(0.0, min(100.0, soc_pct)) / 100,
            min_soc_kwh=capacity * min_soc / 100,
            max_soc_kwh=capacity * max_soc / 100,
            max_charge_kw=float(c[CONF_MAX_CHARGE_W]) / 1000,
            max_discharge_kw=float(c[CONF_MAX_DISCHARGE_W]) / 1000,
            charge_efficiency=float(c[CONF_CHARGE_EFF]) / 100,
            discharge_efficiency=float(c[CONF_DISCHARGE_EFF]) / 100,
            cycle_cost=float(s[SET_CYCLE_COST]) / 100,
        )

    def grid_params(self) -> GridParams:
        """Grid limits."""
        return GridParams(
            max_import_kw=float(self.config[CONF_MAX_IMPORT_W]) / 1000,
            max_export_kw=float(self.config[CONF_MAX_EXPORT_W]) / 1000,
        )

    def strategy(self) -> Strategy:
        """Strategy toggles."""
        s = self.settings
        return Strategy(
            grid_charge=bool(s[SET_GRID_CHARGE]),
            battery_to_grid=bool(s[SET_BATTERY_TO_GRID]),
            curtail_negative=bool(s[SET_CURTAIL_NEGATIVE]),
            arbitrage_margin=float(s[SET_ARBITRAGE_MARGIN]) / 100,
        )

    def _read_prices(self) -> list[PricePoint]:
        state = self.hass.states.get(self.config[CONF_PRICE_ENTITY])
        if state is None:
            return []
        return parse_price_attributes(state.attributes, state.attributes.get("unit_of_measurement"))

    def _read_pv(self) -> list[EnergyInterval]:
        intervals: list[EnergyInterval] = []
        for entity_id in self.config.get(CONF_PV_ENTITIES) or []:
            state = self.hass.states.get(entity_id)
            if state is not None:
                intervals.extend(parse_pv_attributes(state.attributes, self.config[CONF_PV_ESTIMATE]))
        return intervals

    async def _async_load_profile(self, now: datetime) -> LoadProfile:
        tz = dt_util.get_default_time_zone()
        fallback = float(self.config[CONF_LOAD_FALLBACK_W]) / 1000
        if (
            self.load_profile is not None
            and self._load_profile_at is not None
            and now - self._load_profile_at < timedelta(hours=LOAD_PROFILE_REFRESH_H)
        ):
            return self.load_profile
        entity_id = self.config.get(CONF_LOAD_ENTITY)
        profile = LoadProfile(tz=tz, fallback_kw=fallback)
        if entity_id and "recorder" in self.hass.config.components:
            try:
                profile = await self._async_profile_from_statistics(entity_id, now, tz, fallback)
            except Exception as err:
                _LOGGER.warning("Could not build load profile from %s: %s", entity_id, err)
        self.load_profile = profile
        self._load_profile_at = now
        self._update_load_issue(entity_id, profile)
        return profile

    def _update_load_issue(self, entity_id: str | None, profile: LoadProfile) -> None:
        """Flag a load forecast that is only the flat fallback: plans are then far off."""
        issue_id = f"load_fallback_{self.config_entry.entry_id}"
        if profile.kw:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="load_fallback_no_entity" if not entity_id else "load_fallback_no_history",
            translation_placeholders={
                "fallback_w": str(round(float(self.config[CONF_LOAD_FALLBACK_W]))),
                "entity_id": entity_id or "",
            },
        )

    async def _async_profile_from_statistics(
        self, entity_id: str, now: datetime, tz: Any, fallback: float
    ) -> LoadProfile:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        state = self.hass.states.get(entity_id)
        is_energy = state is not None and state.attributes.get("device_class") == "energy"
        units = {"energy": "kWh"} if is_energy else {"power": "kW"}
        stat_type = "change" if is_energy else "mean"
        start = now - timedelta(days=int(self.config[CONF_LOAD_DAYS]))
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, now, {entity_id}, "hour", units, {stat_type}
        )
        samples = []
        for row in stats.get(entity_id, []):
            begin = row["start"]
            if not isinstance(begin, datetime):
                begin = dt_util.utc_from_timestamp(begin)
            value = row.get(stat_type)
            if value is not None:
                samples.append((begin, float(value)))
        _LOGGER.debug("Load profile from %d hourly samples of %s", len(samples), entity_id)
        return build_load_profile(samples, tz, fallback_kw=fallback)

    # --- update ------------------------------------------------------------
    async def _async_update_data(self) -> Plan:
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()

        self.prices = self._read_prices()
        if not self.prices:
            self.last_plan_error = "no_prices"
            raise UpdateFailed(f"No price forecast in {self.config[CONF_PRICE_ENTITY]}")

        soc, reason = soc_reading(self.hass, self.config[CONF_SOC_ENTITY], now)
        if soc is None:
            self.last_plan_error = reason
            raise UpdateFailed(f"SoC unavailable: {self.config[CONF_SOC_ENTITY]}")
        self.soc_pct = soc

        self.pv = self._read_pv()
        load = await self._async_load_profile(now)
        slots = build_slots(
            now,
            self.prices,
            self.pv,
            load,
            self.price_model(),
            tz,
            horizon_hours=HORIZON_HOURS,
            pv_scale=float(self.config[CONF_PV_SCALE]),
        )
        if not slots:
            self.last_plan_error = "no_future_prices"
            raise UpdateFailed("Price forecast does not cover the current time")

        plan = await self.hass.async_add_executor_job(
            partial(
                optimize,
                slots,
                self.battery_params(soc),
                self.grid_params(),
                self.strategy(),
                now=now,
            )
        )
        self.last_plan_error = None
        _LOGGER.debug(
            "Planned %d slots, cost %.2f EUR, savings %.2f EUR",
            len(plan.slots),
            plan.total_cost,
            plan.savings,
        )
        return plan
