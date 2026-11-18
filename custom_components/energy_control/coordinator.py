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
    CONF_EV_POWER_ENTITY,
    CONF_EXPORT_FACTOR,
    CONF_EXPORT_FEE,
    CONF_EXPORT_FIXED,
    CONF_EXPORT_MODE,
    CONF_HP_POWER_ENTITY,
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
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_PRICE_INCLUDES_TAXES,
    CONF_PV_ENTITIES,
    CONF_PV_ESTIMATE,
    CONF_PV_SCALE,
    CONF_SOC_ENTITY,
    CONF_VAT,
    CONF_WEATHER_ENTITY,
    DEFAULT_SETTINGS,
    DEFAULTS,
    DOMAIN,
    HORIZON_HOURS,
    LOAD_PROFILE_REFRESH_H,
    PRICE_ESTIMATE_HOURS,
    REPLAN_MINUTES,
    SET_ARBITRAGE_MARGIN,
    SET_BATTERY_TO_GRID,
    SET_CURTAIL_NEGATIVE,
    SET_CYCLE_COST,
    SET_GRID_CHARGE,
    SET_MAX_SOC,
    SET_MIN_SOC,
    SET_MODE,
    SET_PRICE_ESTIMATES,
    Mode,
)
from .flexible.manager import FlexibleLoads
from .optimizer import Action, BatteryParams, GridParams, Plan, Strategy, optimize
from .price_archive import PriceArchive
from .providers.load import LoadProfile, build_load_profile
from .providers.nordpool import NordPoolForecast
from .providers.price_forecast import PriceEstimator, calibrate_external, hour_key, spot_relation, with_spot
from .providers.prices import ExportMode, PriceModel, PricePoint, parse_price_attributes
from .providers.slots import Corrections, apply_corrections, build_slots
from .providers.solar import EnergyInterval, energy_between, parse_pv_attributes, parse_solcast
from .providers.telemetry import soc_reading
from .providers.util import to_float

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .history import History

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


def _estimate(
    observations: list[Any], weather: dict[datetime, Any], tz: Any, now: datetime, start: datetime, end: datetime
) -> list[PricePoint]:
    """Fit and run the built-in price model (CPU work, runs in the executor)."""
    estimator = PriceEstimator(observations, weather, tz, now)
    return estimator.estimate(start, end, []) if estimator.usable else []


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
        self.history: History | None = None
        self.load_profile: LoadProfile | None = None
        self._load_profile_at: datetime | None = None
        self.override: Override | None = None
        self.prices: list[PricePoint] = []
        self.pv: list[EnergyInterval] = []
        self.nordpool = (
            NordPoolForecast(hass, self.config[CONF_NORDPOOL_ENTRY], self.config[CONF_NORDPOOL_AREA])
            if self.config.get(CONF_NORDPOOL_ENTRY) and self.config.get(CONF_NORDPOOL_AREA)
            else None
        )
        self.soc_pct: float | None = None
        self.last_plan_error: str | None = None
        self.corrections = Corrections()
        self.price_archive = PriceArchive(hass, entry.entry_id)
        self.published: list[PricePoint] = []  # prices without estimates
        self.price_estimate_source: str | None = None
        self.flex = FlexibleLoads(hass, self)

    # --- lifecycle ---------------------------------------------------------
    async def async_setup(self) -> None:
        """Load persisted settings and start listeners."""
        await self.price_archive.async_load()
        await self.flex.async_load()
        stored = await self._settings_store.async_load()
        if stored:
            self.settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})

        # Replan every few minutes (aligned so every quarter-hour boundary is
        # included) to pick up the live SoC after forecast errors.
        self.config_entry.async_on_unload(
            async_track_time_change(
                self.hass, self._on_slot_boundary, minute=list(range(0, 60, REPLAN_MINUTES)), second=3
            )
        )
        watched = [e for e in [self.config.get(CONF_PRICE_ENTITY), *self.pv_forecast_entities()] if e]
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

    async def async_save_settings(self) -> None:
        """Persist settings changed in place (no replan)."""
        await self._settings_store.async_save(self.settings)
        self.async_update_listeners()

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
            includes_taxes=False if self.nordpool else bool(c[CONF_PRICE_INCLUDES_TAXES]),
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
        state = self.hass.states.get(self.config.get(CONF_PRICE_ENTITY, "sensor.none"))
        if state is None:
            return []
        return parse_price_attributes(state.attributes, state.attributes.get("unit_of_measurement"))

    def pv_forecast_entities(self) -> list[str]:
        """Configured forecast sensors plus Solcast's later-day siblings.

        Solcast publishes one sensor per day; configuring only "today" would
        leave every plan past midnight without solar.
        """
        configured = list(self.config.get(CONF_PV_ENTITIES) or [])
        result = list(configured)
        for entity_id in configured:
            if entity_id.endswith("_forecast_today"):
                base = entity_id.removesuffix("_today")
                for suffix in ("_tomorrow", "_day_3"):
                    sibling = base + suffix
                    if sibling not in result and self.hass.states.get(sibling) is not None:
                        result.append(sibling)
        return result

    def _read_pv_band(self) -> tuple[list[EnergyInterval], list[EnergyInterval]]:
        """Solcast's pessimistic (P10) and optimistic (P90) forecasts, if available."""
        low: list[EnergyInterval] = []
        high: list[EnergyInterval] = []
        for entity_id in self.pv_forecast_entities():
            state = self.hass.states.get(entity_id)
            if state is not None:
                low.extend(parse_solcast(state.attributes, "pv_estimate10"))
                high.extend(parse_solcast(state.attributes, "pv_estimate90"))
        return low, high

    def _annotate(self, plan: Plan) -> None:
        """Dashboard extras per slot: PV uncertainty band and wind."""
        low, high = self._read_pv_band()
        scale = float(self.config[CONF_PV_SCALE])
        weather = self.price_archive.weather_map()
        for slot in plan.slots:
            end = slot.start + timedelta(hours=slot.hours)
            if low and high and slot.hours:
                slot.pv_p10_kw = energy_between(low, slot.start, end) * scale / slot.hours
                slot.pv_p90_kw = energy_between(high, slot.start, end) * scale / slot.hours
            w = weather.get(hour_key(slot.start))
            slot.wind_ms = w.wind_ms if w is not None else None

    def _read_pv(self) -> list[EnergyInterval]:
        intervals: list[EnergyInterval] = []
        for entity_id in self.pv_forecast_entities():
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
        """Hourly load history; heat pump and EV are subtracted (planned separately)."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        flexible = [e for e in (self.config.get(CONF_HP_POWER_ENTITY), self.config.get(CONF_EV_POWER_ENTITY)) if e]
        outdoor = self.config.get(CONF_OUTDOOR_TEMP_ENTITY)
        ids = {entity_id, *flexible, *([outdoor] if outdoor else [])}
        start = now - timedelta(days=int(self.config[CONF_LOAD_DAYS]))
        units = {"energy": "kWh", "power": "kW", "temperature": "°C"}
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, now, ids, "hour", units, {"mean", "change"}
        )
        series = {key: self._hourly(stats.get(key, []), key) for key in ids}
        samples = series[entity_id]
        if flexible:
            # Energy per hour (kWh) and mean power (kW) are the same number.
            minus: dict[datetime, float] = {}
            for key in flexible:
                for begin, value in series[key]:
                    minus[begin] = minus.get(begin, 0.0) + value
            samples = [(begin, max(0.0, value - minus.get(begin, 0.0))) for begin, value in samples]
        hp_entity = self.config.get(CONF_HP_POWER_ENTITY)
        if hp_entity and outdoor:
            self.flex.fit_hp_model(series[hp_entity], dict(series[outdoor]))
        elif hp_entity:
            self.flex.fit_hp_model(series[hp_entity], {})
        _LOGGER.debug("Load profile from %d hourly samples of %s", len(samples), entity_id)
        return build_load_profile(samples, tz, fallback_kw=fallback)

    def _hourly(self, rows: list[dict[str, Any]], entity_id: str) -> list[tuple[datetime, float]]:
        """(hour start, value): energy as change per hour, otherwise the mean."""
        state = self.hass.states.get(entity_id)
        stat_type = "change" if state is not None and state.attributes.get("device_class") == "energy" else "mean"
        result = []
        for row in rows:
            begin = row["start"]
            if not isinstance(begin, datetime):
                begin = dt_util.utc_from_timestamp(begin)
            value = row.get(stat_type)
            if value is not None:
                result.append((begin, float(value)))
        return result

    async def _async_price_estimates(self, now: datetime, tz: Any) -> list[PricePoint]:
        """Estimated quarters between the published prices and the estimate horizon."""
        archive = self.price_archive
        archive.add_published(self.published, now)
        await archive.async_update_weather(self.config.get(CONF_WEATHER_ENTITY), self.pv, now)
        if not self.nordpool and self.config.get(CONF_PRICE_ENTITY):
            await archive.async_bootstrap(self.config[CONF_PRICE_ENTITY], now)
        self.price_estimate_source = None
        if not self.settings.get(SET_PRICE_ESTIMATES, True) or not self.published:
            return []
        start = self.published[-1].end
        end = now + timedelta(hours=PRICE_ESTIMATE_HOURS)
        if start >= end:
            return []
        relation = spot_relation(self.published)
        estimates: list[PricePoint] = []
        external_entity = self.config.get(CONF_PRICE_FORECAST_ENTITY)
        if external_entity and (state := self.hass.states.get(external_entity)) is not None:
            try:
                external = parse_price_attributes(state.attributes, state.attributes.get("unit_of_measurement"))
            except ValueError as err:
                _LOGGER.debug("Unusable price forecast %s: %s", external_entity, err)
                external = []
            convert = calibrate_external(external, self.published)
            for point in external:
                when = max(point.start, start)
                while when < min(point.end, end):
                    estimates.append(PricePoint(when, 15, convert(point.price), None, estimated=True))
                    when += timedelta(minutes=15)
            if estimates:
                self.price_estimate_source = "external"
        covered = estimates[-1].end if estimates else start
        if covered < end:
            built_in = await self.hass.async_add_executor_job(
                _estimate, archive.observations(), archive.weather_map(), tz, now, covered, end
            )
            if built_in:
                estimates.extend(built_in)
                self.price_estimate_source = self.price_estimate_source or "built_in"
        # Contiguous quarters only: build_slots stops at a gap.
        result: list[PricePoint] = []
        expected = start
        for point in sorted(estimates, key=lambda p: p.start):
            if point.start != expected:
                break
            result.append(point)
            expected = point.end
        return with_spot(result, relation)

    # --- update ------------------------------------------------------------
    async def _async_update_data(self) -> Plan:
        now = dt_util.utcnow()
        tz = dt_util.get_default_time_zone()

        try:
            self.prices = await self.nordpool.async_prices(now) if self.nordpool else self._read_prices()
        except ValueError as err:
            self.last_plan_error = "invalid_price_unit"
            raise UpdateFailed(str(err)) from err
        if not self.prices:
            self.last_plan_error = "no_prices"
            raise UpdateFailed("No usable price forecast from the configured source")

        self.published = self.prices
        self.pv = self._read_pv()
        self.prices = [*self.published, *await self._async_price_estimates(now, tz)]

        soc, reason = soc_reading(self.hass, self.config[CONF_SOC_ENTITY], now)
        if soc is None:
            self.last_plan_error = reason
            raise UpdateFailed(f"SoC unavailable: {self.config[CONF_SOC_ENTITY]}")
        self.soc_pct = soc

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
        slots = self.flex.with_expected_load(slots)

        if self.history:
            load_kw, pv_kw = self.history.live_average(now)
            self.corrections = Corrections(
                load_factor=self.history.recent_load_factor(now),
                pv_factor=self.history.recent_pv_factor(now),
                live_load_kw=load_kw,
                live_pv_kw=pv_kw,
            )
            slots = apply_corrections(slots, now, self.corrections)
        model = self.price_model()
        missing_spot = model.includes_taxes and any(p.spot is None for p in self.published)
        transition_soon = model.netting_end and model.netting_end <= now.astimezone(tz).date() + timedelta(days=60)
        warn = missing_spot and (
            model.export_mode == ExportMode.SPOT or (model.export_mode == ExportMode.NETTING and transition_soon)
        )
        issue_id = f"export_price_{self.config_entry.entry_id}"
        if warn:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="export_price_missing",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        run = partial(
            optimize, battery=self.battery_params(soc), grid=self.grid_params(), strategy=self.strategy(), now=now
        )
        plan = await self.hass.async_add_executor_job(run, slots)
        # Heat pump and EV are scheduled on the first plan's prices and flows;
        # the battery is then re-planned around their shifted consumption.
        if (adjusted := self.flex.schedule(slots, plan, now)) is not None:
            plan = await self.hass.async_add_executor_job(run, adjusted)
        self.flex.annotate(plan)
        self._annotate(plan)
        self.last_plan_error = "infeasible_grid_constraints" if not plan.feasible else None
        grid_issue = f"grid_constraints_{self.config_entry.entry_id}"
        if plan.feasible:
            ir.async_delete_issue(self.hass, DOMAIN, grid_issue)
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                grid_issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="grid_constraints",
            )
        _LOGGER.debug(
            "Planned %d slots, cost %.2f EUR, savings %.2f EUR",
            len(plan.slots),
            plan.total_cost,
            plan.savings,
        )
        return plan
