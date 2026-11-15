"""Integration tests: setup, entities, websocket, controller takeover/restore."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.energy_control.const import (
    CONF_CAPACITY_KWH,
    CONF_CHARGE_LIMIT_ENTITY,
    CONF_DESS_MODE_ENTITY,
    CONF_DISCHARGE_LIMIT_ENTITY,
    CONF_FEED_IN_LIMIT_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PV_LIMIT_ENTITY,
    CONF_SETPOINT_ENTITY,
    CONF_SOC_ENTITY,
    DOMAIN,
)

PRICE = "sensor.price"
SOC = "sensor.soc"
SETPOINT = "number.gx_device_ac_power_setpoint"
CHARGE = "number.gx_device_ess_max_charge_power_limit"
DISCHARGE = "number.gx_device_ess_max_inverter_power_limit"
FEED_IN = "number.gx_device_ess_max_feed_in_power"
DESS = "select.gx_device_dess_mode"
PV_LIMIT = "number.solis_power_limit"


def set_prices(hass: HomeAssistant, cheap_now: bool = True) -> None:
    """24h of quarter prices: cheap first hour, expensive after."""
    now = dt_util.utcnow()
    start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    prices = [(20.0 if cheap_now else 45.0) if i < 4 else 50.0 for i in range(96)]
    hass.states.async_set(
        PRICE,
        str(prices[0]),
        {
            "unit_of_measurement": "ct/kWh",
            "outlook_quarters": {"start": start.isoformat(), "minutes": 15, "prices": prices},
        },
    )


def set_hardware(hass: HomeAssistant) -> None:
    """Victron-like entities."""
    hass.states.async_set(SOC, "30", {"unit_of_measurement": "%"})
    num = {"min": -1000000, "max": 1000000, "unit_of_measurement": "W"}
    hass.states.async_set(SETPOINT, "0", num)
    hass.states.async_set(CHARGE, "-1", num)
    hass.states.async_set(DISCHARGE, "2750", num)
    hass.states.async_set(FEED_IN, "-1", num)
    hass.states.async_set(PV_LIMIT, "2700", {"min": 0, "max": 10000})
    hass.states.async_set(DESS, "auto_vrm", {"options": ["off", "auto_vrm", "node_red"]})


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A configured entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Energy Control",
        data={
            CONF_PRICE_ENTITY: PRICE,
            CONF_SOC_ENTITY: SOC,
            CONF_CAPACITY_KWH: 10.0,
            CONF_SETPOINT_ENTITY: SETPOINT,
            CONF_CHARGE_LIMIT_ENTITY: CHARGE,
            CONF_DISCHARGE_LIMIT_ENTITY: DISCHARGE,
            CONF_FEED_IN_LIMIT_ENTITY: FEED_IN,
            CONF_DESS_MODE_ENTITY: DESS,
            CONF_PV_LIMIT_ENTITY: PV_LIMIT,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup(hass: HomeAssistant, entry: MockConfigEntry, freezer):
    """Set up the integration with mocked hardware services."""
    # Pin the clock just after a quarter boundary: late in a quarter the (nearly elapsed)
    # current slot is too short to be worth charging and the plan starts in the next one.
    freezer.move_to("2026-11-15T08:01:00+00:00")
    await async_setup_component(hass, "http", {})
    set_prices(hass)
    set_hardware(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Mock after setup: loading our own number/select platforms registers the real services.
    number_calls = async_mock_service(hass, "number", "set_value")
    select_calls = async_mock_service(hass, "select", "select_option")
    return number_calls, select_calls


def written(calls) -> dict[str, float]:
    """Last written value per entity."""
    return {c.data["entity_id"]: c.data["value"] for c in calls}


def accept(hass: HomeAssistant, calls) -> None:
    """Simulate the hardware accepting the written values."""
    for entity_id, value in written(calls).items():
        hass.states.async_set(entity_id, str(value), hass.states.get(entity_id).attributes)


async def test_setup_advisory_creates_plan(hass: HomeAssistant, entry, setup) -> None:
    """Default mode is advisory: a plan, but no hardware writes."""
    number_calls, select_calls = setup
    data = entry.runtime_data
    assert data.coordinator.data is not None
    assert len(data.coordinator.data.slots) > 90
    assert hass.states.get("select.energy_control_mode").state == "advisory"
    assert hass.states.get("sensor.energy_control_action").state in {
        "charge_grid",
        "self_consume",
        "hold",
    }
    # Cheap now, expensive later -> grid charging planned.
    assert hass.states.get("sensor.energy_control_next_grid_charge").state not in ("unknown", None)
    assert not number_calls
    assert not select_calls


async def test_auto_takes_over_and_restores(hass: HomeAssistant, entry, setup) -> None:
    """Auto mode writes ESS settings, switches DESS off; off restores originals."""
    number_calls, select_calls = setup
    # select.select_option is mocked (to capture DESS writes): set the mode directly.
    await entry.runtime_data.coordinator.async_set_setting("mode", "auto")
    await hass.async_block_till_done()
    controller = entry.runtime_data.controller
    assert controller.active
    assert any(c.data == {"entity_id": DESS, "option": "off"} for c in select_calls)
    values = written(number_calls)
    assert values[SETPOINT] == 8000  # charging: high setpoint, capped by charge limit
    assert 0 < values[CHARGE] <= 3000

    accept(hass, number_calls)
    hass.states.async_set(DESS, "off", {"options": ["off", "auto_vrm", "node_red"]})
    number_calls.clear()
    select_calls.clear()

    # select.select_option is mocked (to capture DESS writes): set the mode directly.
    await entry.runtime_data.coordinator.async_set_setting("mode", "off")
    await hass.async_block_till_done()
    assert not controller.active
    restored = written(number_calls)
    assert restored[SETPOINT] == 0
    assert restored[CHARGE] == -1
    assert any(c.data == {"entity_id": DESS, "option": "auto_vrm"} for c in select_calls)


async def test_override_force_discharge(hass: HomeAssistant, entry, setup) -> None:
    """Override service drives a fixed action in control mode."""
    number_calls, _ = setup
    await entry.runtime_data.coordinator.async_set_setting("mode", "auto")
    await hass.async_block_till_done()
    number_calls.clear()
    await hass.services.async_call(
        DOMAIN,
        "override",
        {"action": "discharge_grid", "duration": {"minutes": 30}, "power": 1500},
        blocking=True,
    )
    values = written(number_calls)
    assert values[SETPOINT] < 0
    assert values[DISCHARGE] == 1500


async def test_unload_restores(hass: HomeAssistant, entry, setup) -> None:
    """Unloading hands control back."""
    number_calls, _ = setup
    await entry.runtime_data.coordinator.async_set_setting("mode", "auto")
    await hass.async_block_till_done()
    accept(hass, number_calls)
    number_calls.clear()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert written(number_calls).get(SETPOINT) == 0


async def test_stale_plan_falls_back(hass: HomeAssistant, entry, setup) -> None:
    """Without a fresh plan the controller falls back to self consumption."""
    controller = entry.runtime_data.controller
    action, _, status = controller.decide(dt_util.utcnow())
    assert status == "ok"
    entry.runtime_data.coordinator.data.created -= timedelta(hours=2)
    action, _, status = controller.decide(dt_util.utcnow())
    assert status == "fallback_stale_plan"
    assert str(action) == "self_consume"


async def test_websocket(hass: HomeAssistant, entry, setup, hass_ws_client) -> None:
    """Card API returns plan and history."""
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "energy_control/get"})
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert result["mode"] == "advisory"
    assert result["plan"]["slots"]
    assert "history" in result

    await client.send_json({"id": 2, "type": "energy_control/subscribe"})
    msg = await client.receive_json()
    assert msg["success"]
    event = await client.receive_json()
    assert event["type"] == "event"
    assert event["event"]["plan"]["slots"]
