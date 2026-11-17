"""Integration tests: setup, entities, websocket, controller takeover/restore."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.energy_control.const import DOMAIN

from .common import CHARGE, DESS, DISCHARGE, SETPOINT, accept, written


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
    assert str(action) == "hold"


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


async def test_brand_icon_served(hass: HomeAssistant, hass_client) -> None:
    """The integration ships its own brand icons (HA serves them before the CDN)."""
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()
    for image in ("icon.png", "icon@2x.png", "dark_icon.png", "logo.png"):
        response = await client.get(f"/api/brands/integration/{DOMAIN}/{image}")
        assert response.status == 200, image
        assert (await response.read()).startswith(b"\x89PNG")
