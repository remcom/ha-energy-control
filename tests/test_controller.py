"""Controller edge cases: takeover, restore, restart, shutdown, overrides."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.energy_control.config_flow import victron_suggestions
from custom_components.energy_control.const import (
    CONF_LOAD_ENTITY,
    CONF_PV_LIMIT_ENTITY,
    CONF_SETPOINT_ENTITY,
    DOMAIN,
)
from custom_components.energy_control.history import History
from custom_components.energy_control.optimizer import Action, SlotPlan
from custom_components.energy_control.providers.load import LoadProfile

from .common import (
    CHARGE,
    DESS,
    DISCHARGE,
    PV_LIMIT,
    SETPOINT,
    accept,
    set_hardware,
    set_prices,
    written,
)

NOW = "2026-11-15T08:01:00+00:00"


async def _auto(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.coordinator.async_set_setting("mode", "auto")
    await hass.async_block_till_done()


def _set(hass: HomeAssistant, entity_id: str, state: str, **attrs) -> None:
    current = hass.states.get(entity_id)
    hass.states.async_set(entity_id, state, {**(current.attributes if current else {}), **attrs})


@pytest.fixture
async def restart_setup(hass: HomeAssistant, entry, freezer, hass_storage):
    """Set up as after a crash: originals still stored, mode auto, PV curtailed."""
    freezer.move_to(NOW)
    hass_storage[f"{DOMAIN}.{entry.entry_id}.settings"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.settings",
        "data": {"mode": "auto"},
    }
    hass_storage[f"{DOMAIN}.{entry.entry_id}.originals"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.originals",
        "data": {
            "setpoint_entity": 0.0,
            "charge_limit_entity": -1.0,
            "discharge_limit_entity": 2750.0,
            "feed_in_limit_entity": -1.0,
            "pv_limit_entity": 2700.0,
            "dess_mode_entity": "auto_vrm",
            "_curtailing": True,
        },
    }
    await async_setup_component(hass, "http", {})
    # Set up the number/select domains first so the mocks are not replaced later.
    await async_setup_component(hass, "number", {})
    await async_setup_component(hass, "select", {})
    number_calls = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "select", "select_option")
    set_prices(hass)
    set_hardware(hass)
    _set(hass, PV_LIMIT, "0")  # still curtailed from before the crash
    _set(hass, DESS, "off")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return number_calls


# --- takeover ------------------------------------------------------------------
async def test_takeover_waits_for_unavailable_actuator(hass: HomeAssistant, entry, setup) -> None:
    """No takeover (and no half-saved originals) while an actuator is unavailable."""
    number_calls, select_calls = setup
    _set(hass, DISCHARGE, STATE_UNAVAILABLE)
    await _auto(hass, entry)
    controller = entry.runtime_data.controller
    assert not controller.active
    assert controller.status == "waiting_for_entities"
    assert not number_calls
    assert not select_calls

    _set(hass, DISCHARGE, "2750")
    await controller.async_apply()
    assert controller.active
    accept(hass, number_calls)
    _set(hass, DISCHARGE, "0")  # e.g. after a hold slot
    number_calls.clear()

    await entry.runtime_data.coordinator.async_set_setting("mode", "off")
    await hass.async_block_till_done()
    assert not controller.active
    assert written(number_calls)[DISCHARGE] == 2750


async def test_pv_limit_original_captured_late(hass: HomeAssistant, entry, setup) -> None:
    """An offline PV inverter does not block takeover; its original is saved once it reports."""
    _set(hass, PV_LIMIT, STATE_UNAVAILABLE)
    await _auto(hass, entry)
    controller = entry.runtime_data.controller
    assert controller.active
    assert CONF_PV_LIMIT_ENTITY not in controller._originals

    _set(hass, PV_LIMIT, "2500")
    await controller.async_apply()
    assert controller._originals[CONF_PV_LIMIT_ENTITY] == 2500


# --- release -------------------------------------------------------------------
async def test_release_continues_after_a_failure(hass: HomeAssistant, entry, setup, hass_storage) -> None:
    """One missing entity does not stop the other restores; control is kept for a retry."""
    number_calls, select_calls = setup
    await _auto(hass, entry)
    accept(hass, number_calls)
    _set(hass, DESS, "off")
    _set(hass, DISCHARGE, "0")  # e.g. after a hold slot
    number_calls.clear()
    select_calls.clear()

    removed = hass.states.get(CHARGE)
    hass.states.async_remove(CHARGE)
    await entry.runtime_data.coordinator.async_set_setting("mode", "off")
    await hass.async_block_till_done()
    controller = entry.runtime_data.controller
    restored = written(number_calls)
    assert restored[SETPOINT] == 0
    assert restored[DISCHARGE] == 2750
    assert any(c.data == {"entity_id": DESS, "option": "auto_vrm"} for c in select_calls)
    assert controller.active  # not everything restored: keep the originals
    assert f"{DOMAIN}.{entry.entry_id}.originals" in hass_storage

    hass.states.async_set(CHARGE, removed.state, removed.attributes)
    number_calls.clear()
    await controller.async_apply()
    assert not controller.active
    assert written(number_calls)[CHARGE] == -1


# --- restart / shutdown ----------------------------------------------------------
async def test_restart_while_curtailing_restores_pv_limit(hass: HomeAssistant, entry, restart_setup) -> None:
    """After a crash mid-curtailment the PV limit is restored once curtailing ends."""
    number_calls = restart_setup
    controller = entry.runtime_data.controller
    assert controller.active
    assert not controller.curtailing  # positive prices: curtailment ended
    assert written(number_calls)[PV_LIMIT] == 2700


async def test_no_takeover_after_shutdown(hass: HomeAssistant, entry, setup) -> None:
    """After the stop event the controller releases and stays released."""
    number_calls, _ = setup
    await _auto(hass, entry)
    accept(hass, number_calls)
    _set(hass, DESS, "off")
    controller = entry.runtime_data.controller
    assert controller.active

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not controller.active

    entry.runtime_data.coordinator.async_update_listeners()
    await controller.async_apply()
    await hass.async_block_till_done()
    assert not controller.active


# --- writers -------------------------------------------------------------------
async def test_out_of_range_target_is_blocked(hass: HomeAssistant, entry, setup) -> None:
    """An unexecutable target is reported before any actuator is changed."""
    number_calls, _ = setup
    _set(hass, SETPOINT, "0", max=5000)
    await _auto(hass, entry)
    assert not number_calls
    assert entry.runtime_data.controller.status == "error"
    assert "outside max" in entry.runtime_data.controller.last_error


async def test_kw_entity_written_in_kw(hass: HomeAssistant, entry, setup) -> None:
    """Values for a kW number entity are converted from W."""
    number_calls, _ = setup
    hass.states.async_set(SETPOINT, "0", {"min": -100, "max": 100, "unit_of_measurement": "kW"})
    await _auto(hass, entry)
    assert written(number_calls)[SETPOINT] == 8.0


# --- overrides -------------------------------------------------------------------
async def test_override_respects_min_soc(hass: HomeAssistant, entry, setup) -> None:
    """A discharge override stops at the minimum SoC like the force-discharge mode."""
    coordinator = entry.runtime_data.coordinator
    await _auto(hass, entry)
    await coordinator.async_set_setting("min_soc", 40.0)  # SoC is 30 %
    await hass.async_block_till_done()
    await hass.services.async_call(
        DOMAIN, "override", {"action": "discharge_grid", "duration": {"minutes": 30}}, blocking=True
    )
    action, _, status = entry.runtime_data.controller.decide(dt_util.utcnow())
    assert action == Action.HOLD
    assert status == "min_soc_reached"


async def test_override_requires_control_mode(hass: HomeAssistant, entry, setup) -> None:
    """In advisory mode an override would be ignored: reject it."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "override", {"action": "charge_grid", "duration": {"minutes": 30}}, blocking=True
        )
    assert entry.runtime_data.coordinator.override is None


async def test_override_power_is_inverter_limit(hass: HomeAssistant, entry, setup) -> None:
    """The requested discharge power ends up as the inverter limit unchanged."""
    number_calls, _ = setup
    await _auto(hass, entry)
    await hass.services.async_call(
        DOMAIN, "override", {"action": "discharge_grid", "duration": {"minutes": 30}, "power": 1500}, blocking=True
    )
    assert written(number_calls)[DISCHARGE] == 1500


# --- settings / options ------------------------------------------------------------
async def test_min_soc_above_max_rejected(hass: HomeAssistant, entry, setup) -> None:
    """Number entities refuse an inverted SoC window."""
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_set_setting("max_soc", 80.0)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_min_soc")
    entity = hass.data["entity_components"]["number"].get_entity(entity_id)
    with pytest.raises(ServiceValidationError):
        await entity.async_set_native_value(90.0)
    assert coordinator.settings["min_soc"] == 15.0


async def test_options_can_clear_an_entity(hass: HomeAssistant, entry, setup) -> None:
    """A field removed in the options flow stays removed (not taken from entry.data)."""
    options = {k: v for k, v in entry.data.items() if k != CONF_PV_LIMIT_ENTITY}
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()  # update listener reloads the entry
    config = entry.runtime_data.coordinator.config
    assert CONF_PV_LIMIT_ENTITY not in config
    assert config[CONF_SETPOINT_ENTITY] == SETPOINT


# --- sensors / websocket -----------------------------------------------------------
async def test_next_grid_charge_is_current_slot(hass: HomeAssistant, entry, setup) -> None:
    """While a charge slot is running, 'next grid charge' is that slot."""
    current = entry.runtime_data.coordinator.data.slot_at(dt_util.utcnow())
    assert current.action == Action.CHARGE_GRID
    state = hass.states.get("sensor.energy_control_next_grid_charge")
    assert dt_util.parse_datetime(state.state) == current.start.replace(microsecond=0)


async def test_websocket_follows_reload(hass: HomeAssistant, entry, setup, hass_ws_client) -> None:
    """A dashboard subscription keeps receiving updates after the entry reloads."""
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "energy_control/subscribe"})
    assert (await client.receive_json())["success"]
    await client.receive_json()  # initial payload

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.coordinator.async_set_setting("mode", "off")
    await hass.async_block_till_done()

    async with asyncio.timeout(5):
        while (await client.receive_json())["event"]["mode"] != "off":
            pass


# --- history -----------------------------------------------------------------------
async def test_history_prices_import_and_export_separately(hass: HomeAssistant, freezer) -> None:
    """A slot with both import and export is not priced on its net average."""
    freezer.move_to("2026-11-15T08:00:00+00:00")
    history = History(hass, "test")
    planned = SlotPlan(
        start=dt_util.utcnow(),
        hours=0.25,
        action=Action.SELF_CONSUME,
        battery_kw=0,
        grid_kw=0,
        pv_kw=0,
        load_kw=0,
        curtail_kw=0,
        soc_start_pct=50,
        soc_end_pct=50,
        import_price=0.30,
        export_price=0.05,
        cost=0,
    )
    t0 = dt_util.utcnow()
    # 7.5 min importing 2 kW, 7.5 min exporting 2 kW: net zero.
    for i in range(90):
        now = t0 + timedelta(seconds=10 * i)
        grid = 2000.0 if i < 45 else -2000.0
        history.sample(now, mode="auto", planned=planned, grid_w=grid, battery_w=0, soc_pct=50, curtailing=False)
    closed = history.sample(
        t0 + timedelta(minutes=15, seconds=1),
        mode="auto",
        planned=planned,
        grid_w=0,
        battery_w=0,
        soc_pct=50,
        curtailing=False,
    )
    assert closed is not None
    # ~0.25 kWh imported at 0.30 and ~0.25 kWh exported at 0.05 -> ~0.0625 EUR, not 0.
    assert closed["actual_cost"] == pytest.approx(0.0625, abs=0.003)


# --- config flow suggestions ---------------------------------------------------------
def _victron(hass: HomeAssistant, domain: str, object_id: str) -> None:
    er.async_get(hass).async_get_or_create(domain, "victron_gx", object_id, suggested_object_id=object_id)


async def test_suggestions_skip_l1_on_three_phase(hass: HomeAssistant) -> None:
    """Per-phase (L1) sensors are not suggested when L2 exists."""
    _victron(hass, "sensor", "gx_consumption_power_l1")
    _victron(hass, "sensor", "gx_consumption_power_l2")
    _victron(hass, "number", "gx_ac_power_setpoint")
    found = victron_suggestions(hass)
    assert CONF_LOAD_ENTITY not in found
    assert found[CONF_SETPOINT_ENTITY] == "number.gx_ac_power_setpoint"


async def test_suggestions_use_l1_on_single_phase(hass: HomeAssistant) -> None:
    """On a single-phase system the L1 sensor is the whole system."""
    _victron(hass, "sensor", "gx_consumption_power_l1")
    assert victron_suggestions(hass)[CONF_LOAD_ENTITY] == "sensor.gx_consumption_power_l1"


async def test_suggestions_use_l1_when_gx_l2_reads_zero(hass: HomeAssistant) -> None:
    """A single-phase GX also exposes L2 sensors, reading 0: L1 is still the whole system."""
    _victron(hass, "sensor", "gx_consumption_power_l1")
    _victron(hass, "sensor", "gx_consumption_power_l2")
    hass.states.async_set("sensor.gx_consumption_power_l2", "0.0", {"unit_of_measurement": "W"})
    assert victron_suggestions(hass)[CONF_LOAD_ENTITY] == "sensor.gx_consumption_power_l1"

    hass.states.async_set("sensor.gx_consumption_power_l2", "350", {"unit_of_measurement": "W"})
    assert CONF_LOAD_ENTITY not in victron_suggestions(hass)


# --- load forecast -----------------------------------------------------------------
async def test_repair_issue_when_load_forecast_is_fallback(hass: HomeAssistant, entry, setup) -> None:
    """Without consumption history the flat fallback is flagged, and the flag clears with history."""
    issue_id = f"load_fallback_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "load_fallback_no_entity"

    coordinator = entry.runtime_data.coordinator
    coordinator._update_load_issue("sensor.load", LoadProfile(tz=dt_util.UTC, kw={(False, 12): 0.9}))
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# --- reload ------------------------------------------------------------------------
async def test_options_reload_keeps_control(hass: HomeAssistant, entry, setup) -> None:
    """Saving options that leave the hardware alone does not restore + retake the ESS."""
    number_calls, select_calls = setup
    await _auto(hass, entry)
    accept(hass, number_calls)
    _set(hass, DESS, "off")
    originals = dict(entry.runtime_data.controller._originals)
    number_calls.clear()
    select_calls.clear()

    hass.config_entries.async_update_entry(entry, options={**entry.data, "pv_scale": 0.9})
    await hass.async_block_till_done()

    controller = entry.runtime_data.controller
    assert controller.active
    assert controller._originals == originals  # not re-captured from the forced values
    assert not select_calls  # DESS not switched back on
    assert SETPOINT not in written(number_calls) or written(number_calls)[SETPOINT] != 0


async def test_options_reload_with_new_hardware_hands_over(hass: HomeAssistant, entry, setup) -> None:
    """A replaced actuator gets its original back; the others stay under control."""
    number_calls, select_calls = setup
    await _auto(hass, entry)
    accept(hass, number_calls)
    _set(hass, DESS, "off")
    _set(hass, PV_LIMIT, "1200")  # e.g. curtailed earlier
    number_calls.clear()
    select_calls.clear()
    new_limiter = "number.other_pv_limit"
    hass.states.async_set(new_limiter, "2000", {"min": 0, "max": 10000})

    hass.config_entries.async_update_entry(entry, options={**entry.data, CONF_PV_LIMIT_ENTITY: new_limiter})
    await hass.async_block_till_done()

    controller = entry.runtime_data.controller
    assert controller.active
    assert written(number_calls)[PV_LIMIT] == 2700  # old limiter restored
    assert DISCHARGE not in written(number_calls)  # not restored: still controlled
    assert not select_calls  # DESS stays off
    assert controller._originals[CONF_PV_LIMIT_ENTITY] == 2000  # new limiter captured
    assert controller._originals["discharge_limit_entity"] == 2750  # kept, not re-captured


async def test_new_actuator_not_written_before_capture(hass: HomeAssistant, entry, setup) -> None:
    """An actuator added by a reload while offline is only written once its original is saved."""
    number_calls, _ = setup
    await _auto(hass, entry)
    accept(hass, number_calls)
    number_calls.clear()
    new_charge = "number.other_charge_limit"
    hass.states.async_set(new_charge, STATE_UNAVAILABLE, {"min": -1, "max": 10000})

    hass.config_entries.async_update_entry(entry, options={**entry.data, "charge_limit_entity": new_charge})
    await hass.async_block_till_done()
    controller = entry.runtime_data.controller
    assert new_charge not in written(number_calls)
    assert "charge_limit_entity" not in controller._originals

    _set(hass, new_charge, "-1")
    await controller.async_apply()
    assert controller._originals["charge_limit_entity"] == -1
    assert new_charge in written(number_calls)


async def test_remove_entry_deletes_stores(hass: HomeAssistant, entry, setup, hass_storage) -> None:
    """Removing the entry leaves no settings or history behind."""
    await entry.runtime_data.coordinator.async_set_setting("min_soc", 20.0)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}.settings" in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not [k for k in hass_storage if k.startswith(f"{DOMAIN}.{entry.entry_id}.")]


# --- drift replan ------------------------------------------------------------------
async def test_no_drift_replan_while_planning_fails(hass: HomeAssistant, entry, setup, freezer) -> None:
    """A failing coordinator keeps its old plan; SoC drift must not retry every tick."""
    controller = entry.runtime_data.controller
    coordinator = entry.runtime_data.coordinator
    requests = []

    async def fake_refresh() -> None:
        requests.append(1)

    coordinator.async_request_refresh = fake_refresh
    freezer.tick(timedelta(minutes=2))
    _set(hass, "sensor.soc", "80")  # far from the planned ~30 %

    coordinator.last_update_success = False
    controller._record(dt_util.utcnow())
    await hass.async_block_till_done()
    assert not requests

    coordinator.last_update_success = True
    controller._record(dt_util.utcnow())
    await hass.async_block_till_done()
    assert requests


async def test_replans_every_five_minutes(hass: HomeAssistant, entry, setup, freezer) -> None:
    """Periodic replans also run between quarter-hour boundaries."""
    coordinator = entry.runtime_data.coordinator
    requests = []

    async def fake_refresh() -> None:
        requests.append(dt_util.utcnow())

    coordinator.async_request_refresh = fake_refresh
    now = dt_util.utcnow()
    target = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0) + timedelta(minutes=5)
    if target.minute % 15 == 0:
        target += timedelta(minutes=5)  # make sure this is not a slot boundary
    freezer.move_to(target + timedelta(seconds=3))
    async_fire_time_changed(hass, target + timedelta(seconds=3))
    await hass.async_block_till_done()
    assert requests


async def test_solcast_later_days_added_automatically(hass: HomeAssistant, entry, setup) -> None:
    coordinator = entry.runtime_data.coordinator
    coordinator.config["pv_entities"] = ["sensor.solcast_pv_forecast_forecast_today"]
    _set(hass, "sensor.solcast_pv_forecast_forecast_today", "1.2")
    _set(hass, "sensor.solcast_pv_forecast_forecast_tomorrow", "1.1")
    assert coordinator.pv_forecast_entities() == [
        "sensor.solcast_pv_forecast_forecast_today",
        "sensor.solcast_pv_forecast_forecast_tomorrow",
    ]


async def test_pv_production_is_summed_and_unknown_when_partial(hass: HomeAssistant, entry, setup) -> None:
    controller = entry.runtime_data.controller
    controller.config["pv_production_entities"] = ["sensor.pv_ac", "sensor.pv_dc"]
    _set(hass, "sensor.pv_ac", "800", unit_of_measurement="W")
    _set(hass, "sensor.pv_dc", "1.2", unit_of_measurement="kW")
    assert controller._pv_production() == 2000
    _set(hass, "sensor.pv_dc", STATE_UNAVAILABLE)
    assert controller._pv_production() is None


async def test_cheapest_window_service(hass: HomeAssistant, entry, setup) -> None:
    result = await hass.services.async_call(
        DOMAIN, "cheapest_window", {"duration": {"hours": 1}, "power": 2000}, blocking=True, return_response=True
    )
    assert result["energy_kwh"] == 2.0
    assert dt_util.parse_datetime(result["start"]) < dt_util.parse_datetime(result["end"])
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "cheapest_window", {"duration": {"hours": 200}}, blocking=True, return_response=True
        )


async def test_estimated_price_slot_falls_back_to_self_consumption(hass: HomeAssistant, entry, setup) -> None:
    controller = entry.runtime_data.controller
    coordinator = entry.runtime_data.coordinator
    plan = coordinator.data
    plan.slots[0].price_estimated = True
    await coordinator.async_set_setting("mode", "auto")
    coordinator.async_set_updated_data(plan)
    action, slot, status = controller._propose(dt_util.utcnow())
    assert (action, slot, status) == (Action.SELF_CONSUME, None, "fallback_estimated_price")


async def test_built_in_estimates_extend_the_plan(hass: HomeAssistant, entry, setup) -> None:
    coordinator = entry.runtime_data.coordinator
    archive = coordinator.price_archive
    now = dt_util.utcnow()
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(days=5)
    for i in range(5 * 96):
        when = start + timedelta(minutes=15 * i)
        hour = when.hour
        archive.prices[when.isoformat()] = (0.20 + (0.15 if 16 <= hour < 20 else 0.0), None)
    await coordinator.async_refresh()
    plan = coordinator.data
    assert coordinator.price_estimate_source == "built_in"
    assert plan.estimated_from is not None
    assert plan.slots[-1].start >= now + timedelta(hours=35)

    await coordinator.async_set_setting("price_estimates", False)
    await coordinator.async_refresh()
    assert coordinator.data.estimated_from is None
