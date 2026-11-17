"""Regression tests for live protection, capability failures and DESS ownership."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.energy_control.const import DOMAIN, Mode
from custom_components.energy_control.control.victron import VictronController
from custom_components.energy_control.coordinator import Override
from custom_components.energy_control.optimizer import Action

from .common import CHARGE, DESS, DISCHARGE, FEED_IN, SETPOINT, SOC, accept, written


def set_value(hass, entity_id, value, **attrs):
    old = hass.states.get(entity_id)
    hass.states.async_set(entity_id, str(value), {**(old.attributes if old else {}), **attrs})


@pytest.mark.parametrize("mode", [Mode.AUTO, Mode.FORCE_CHARGE, Mode.FORCE_DISCHARGE, Mode.SELF_CONSUMPTION])
@pytest.mark.parametrize("soc", ["unavailable", "nan", "inf", "-1", "101"])
async def test_invalid_live_soc_blocks_both_directions(hass, entry, setup, mode, soc):
    calls, _ = setup
    c = entry.runtime_data.coordinator
    c.settings["mode"] = mode
    set_value(hass, SOC, soc)
    await entry.runtime_data.controller.async_apply()
    controller = entry.runtime_data.controller
    assert controller.status.startswith("fallback_")
    assert controller.desired.action == Action.HOLD
    assert written(calls)[CHARGE] == 0
    assert written(calls)[DISCHARGE] == 0


@pytest.mark.parametrize(
    "action,soc,blocked", [(Action.CHARGE_GRID, 100, CHARGE), (Action.DISCHARGE_GRID, 15, DISCHARGE)]
)
async def test_automatic_action_stops_at_live_limit_and_recovers(hass, entry, setup, action, soc, blocked):
    calls, _ = setup
    c = entry.runtime_data.coordinator
    c.settings["mode"] = Mode.AUTO
    slot = c.data.slot_at(dt_util.utcnow())
    slot.action = action
    slot.battery_kw = 2 if action == Action.CHARGE_GRID else -2
    set_value(hass, SOC, soc)
    await entry.runtime_data.controller.async_apply()
    controller = entry.runtime_data.controller
    assert controller.status in ("min_soc_reached", "max_soc_reached")
    assert controller.desired.action == Action.HOLD
    assert written(calls)[blocked] == 0
    await hass.async_block_till_done()
    set_value(hass, SOC, 50)
    c.settings["mode"] = Mode.FORCE_CHARGE if action == Action.CHARGE_GRID else Mode.FORCE_DISCHARGE
    await controller.async_apply()
    assert controller.desired.action == action
    assert controller.status == "manual"


@pytest.mark.parametrize("action", [Action.CHARGE_GRID, Action.DISCHARGE_GRID])
async def test_override_requires_live_soc(hass, entry, setup, action):
    c = entry.runtime_data.coordinator
    c.settings["mode"] = Mode.AUTO
    c.override = Override(action, dt_util.utcnow() + timedelta(minutes=30))
    set_value(hass, SOC, "unavailable")
    await entry.runtime_data.controller.async_apply()
    assert entry.runtime_data.controller.desired.charge_limit == 0
    assert entry.runtime_data.controller.desired.discharge_limit == 0


async def test_stale_soc_and_fresh_unchanged_reports(hass, entry, setup, freezer):
    c = entry.runtime_data.coordinator
    c.settings["mode"] = Mode.FORCE_CHARGE
    freezer.tick(timedelta(minutes=6))
    await entry.runtime_data.controller.async_apply()
    assert entry.runtime_data.controller.status == "fallback_stale_soc"
    # Re-reporting the same value refreshes last_reported, not last_changed.
    set_value(hass, SOC, 30)
    await entry.runtime_data.controller.async_apply()
    assert entry.runtime_data.controller.status == "manual"
    set_value(hass, SOC, 30, source_timestamp="2026-11-15T07:00:00+00:00")
    await entry.runtime_data.controller.async_apply()
    assert entry.runtime_data.controller.status == "fallback_stale_soc"


async def test_dess_service_failure_blocks_all_ess_writes(hass, entry, setup):
    calls, _ = setup
    c = entry.runtime_data.coordinator
    c.settings["mode"] = Mode.FORCE_CHARGE
    controller = entry.runtime_data.controller
    with patch.object(controller, "_select", AsyncMock(side_effect=HomeAssistantError("DESS failed"))):
        await controller.async_apply()
    assert not calls
    assert controller.status == "error"
    assert "DESS failed" in controller.last_error
    assert not controller.controlling
    assert controller._originals["dess_mode_entity"] == "auto_vrm"


async def test_dess_delayed_ack_retries_bounded_and_detects_reenable(hass, entry, setup, freezer):
    calls, _ = setup
    entry.runtime_data.coordinator.settings["mode"] = Mode.FORCE_CHARGE
    controller = entry.runtime_data.controller
    with patch.object(controller, "_select", AsyncMock()) as select:
        await controller.async_apply()
        await controller.async_apply()
        assert select.await_count == 1
        assert not calls
        freezer.tick(timedelta(seconds=31))
        await controller.async_apply()
        assert select.await_count == 2
        set_value(hass, DESS, "off")
        await controller.async_apply()
        assert controller.status == "manual"
        assert written(calls)[SETPOINT] == 8000
        accept(hass, calls)
        calls.clear()
        set_value(hass, DESS, "auto_vrm")
        await controller.async_apply()
        assert controller.status == "error"
        # Previous high setpoint is neutralized while ownership is uncertain.
        assert written(calls)[SETPOINT] == 30
        assert written(calls)[CHARGE] == 0


async def test_unsupported_dess_off_option_blocks_control(hass, entry, setup):
    calls, _ = setup
    set_value(hass, DESS, "enabled", options=["enabled"])
    entry.runtime_data.coordinator.settings["mode"] = Mode.FORCE_CHARGE
    await entry.runtime_data.controller.async_apply()
    assert not calls
    assert "No supported DESS off option" in entry.runtime_data.controller.last_error


@pytest.mark.parametrize("key", ["charge_limit_entity", "discharge_limit_entity", "feed_in_limit_entity"])
async def test_incomplete_actuators_remain_advisory_with_explanation(hass, entry, setup, key):
    calls, _ = setup
    c = entry.runtime_data.coordinator
    c.config.pop(key)
    c.settings["mode"] = Mode.AUTO
    await entry.runtime_data.controller.async_apply()
    assert not calls
    assert "Advisory only" in entry.runtime_data.controller.last_error
    assert key in entry.runtime_data.controller.last_error


@pytest.mark.parametrize("entity", [CHARGE, DISCHARGE, FEED_IN])
async def test_lost_essential_actuator_cannot_report_success(hass, entry, setup, entity):
    entry.runtime_data.coordinator.settings["mode"] = Mode.AUTO
    await entry.runtime_data.controller.async_apply()
    set_value(hass, entity, "unavailable")
    await entry.runtime_data.controller.async_apply()
    assert entry.runtime_data.controller.status == "error"
    assert entry.runtime_data.controller.desired is None


async def test_bad_actuator_unit_blocks_before_write(hass, entry, setup):
    calls, _ = setup
    set_value(hass, CHARGE, -1, unit_of_measurement="A")
    entry.runtime_data.coordinator.settings["mode"] = Mode.AUTO
    await entry.runtime_data.controller.async_apply()
    assert not calls
    assert "Unsupported power unit" in entry.runtime_data.controller.last_error


async def test_replaced_actuator_restore_survives_reload_and_restart(hass, entry, setup, freezer, hass_storage):
    calls, _ = setup
    c = entry.runtime_data.coordinator
    c.settings["mode"] = Mode.AUTO
    await entry.runtime_data.controller.async_apply()
    accept(hass, calls)
    calls.clear()
    set_value(hass, CHARGE, "unavailable")
    new_charge = "number.new_charge"
    hass.states.async_set(new_charge, "-1", {"unit_of_measurement": "W", "min": -1, "max": 10000})
    hass.config_entries.async_update_entry(entry, options={**entry.data, "charge_limit_entity": new_charge})
    await hass.async_block_till_done()
    controller = entry.runtime_data.controller
    assert CHARGE in controller.pending_restores
    assert controller.pending_restores[CHARGE]["unit"] == "W"
    assert f"{DOMAIN}.{entry.entry_id}.pending_restores" in hass_storage
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"pending_restores_{entry.entry_id}")

    restarted = VictronController(hass, entry.runtime_data.coordinator, entry.runtime_data.history)
    await restarted.async_load()
    assert CHARGE in restarted.pending_restores
    set_value(hass, CHARGE, "0")
    freezer.tick(timedelta(seconds=31))
    await restarted.async_apply()
    assert written(calls)[CHARGE] == -1
    assert CHARGE in restarted.pending_restores  # service success still needs readback
    accept(hass, calls)
    freezer.tick(timedelta(seconds=31))
    await restarted.async_apply()
    assert not restarted.pending_restores
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"pending_restores_{entry.entry_id}") is None


async def test_export_transition_repair_and_unknown_export_override(hass, entry, setup, freezer):
    c = entry.runtime_data.coordinator
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"export_price_{entry.entry_id}") is not None
    freezer.move_to("2027-01-01T08:01:00+00:00")
    from .common import set_prices

    set_value(hass, SOC, 50)
    set_prices(hass)
    await c.async_refresh()
    assert not c.data.export_prices_complete
    assert c.data.as_dict()["savings"] is None
    assert c.data.as_dict()["slots"][0]["export_price"] is None
    c.settings["mode"] = Mode.FORCE_DISCHARGE
    await entry.runtime_data.controller.async_apply()
    assert entry.runtime_data.controller.status == "fallback_export_price_unknown"
    assert entry.runtime_data.controller.desired.discharge_limit == 0
    c.config["export_mode"] = "fixed"
    c.config["export_fixed"] = 0.05
    await c.async_refresh()
    await entry.runtime_data.controller.async_apply()
    assert c.data.export_prices_complete
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"export_price_{entry.entry_id}") is None
    assert entry.runtime_data.controller.desired.action == Action.DISCHARGE_GRID
