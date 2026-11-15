"""Websocket API used by the dashboard card."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import CONF_CAPACITY_KWH, DOMAIN, SET_MAX_SOC, SET_MIN_SOC
from .data import EnergyControlData


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    """Register commands."""
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_subscribe)


def _find(hass: HomeAssistant, entry_id: str | None) -> EnergyControlData | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and (entry_id is None or entry.entry_id == entry_id):
            return entry.runtime_data
    return None


def build_payload(data: EnergyControlData, history_hours: float) -> dict[str, Any]:
    """Everything the card needs."""
    coordinator = data.coordinator
    controller = data.controller
    plan = coordinator.data
    since = dt_util.utcnow() - timedelta(hours=history_hours)
    desired = controller.desired
    return {
        "entry_id": coordinator.config_entry.entry_id,
        "title": coordinator.config_entry.title,
        "mode": coordinator.mode.value,
        "status": controller.status,
        "control_active": controller.active,
        "curtailing": controller.curtailing,
        "last_error": controller.last_error or coordinator.last_plan_error,
        "soc": coordinator.soc_pct,
        "settings": dict(coordinator.settings),
        "capacity_kwh": float(coordinator.config[CONF_CAPACITY_KWH]),
        "min_soc": coordinator.settings[SET_MIN_SOC],
        "max_soc": coordinator.settings[SET_MAX_SOC],
        "override": (
            {"action": str(coordinator.override.action), "until": coordinator.override.until.isoformat()}
            if coordinator.override
            else None
        ),
        "desired": (
            {
                "action": str(desired.action),
                "setpoint": desired.setpoint,
                "charge_limit": desired.charge_limit,
                "discharge_limit": desired.discharge_limit,
                "feed_in_limit": desired.feed_in_limit,
            }
            if desired
            else None
        ),
        "plan": plan.as_dict() if plan else None,
        "history": data.history.since(since),
        "cost_today": round(data.history.actual_cost_since(dt_util.start_of_local_day()), 3),
        "planned_cost_today": round(data.history.planned_cost_since(dt_util.start_of_local_day()), 3),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_control/get",
        vol.Optional("entry_id"): str,
        vol.Optional("history_hours", default=24): vol.Coerce(float),
    }
)
@callback
def ws_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """One-shot fetch."""
    data = _find(hass, msg.get("entry_id"))
    if data is None:
        connection.send_error(msg["id"], "not_found", "No loaded Energy Control entry")
        return
    connection.send_result(msg["id"], build_payload(data, msg["history_hours"]))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "energy_control/subscribe",
        vol.Optional("entry_id"): str,
        vol.Optional("history_hours", default=24): vol.Coerce(float),
    }
)
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Push the payload on every plan or controller change."""
    data = _find(hass, msg.get("entry_id"))
    if data is None:
        connection.send_error(msg["id"], "not_found", "No loaded Energy Control entry")
        return
    hours = msg["history_hours"]
    last: dict[str, Any] = {}

    @callback
    def push() -> None:
        payload = build_payload(data, hours)
        if payload != last.get("payload"):
            last["payload"] = payload
            connection.send_message(websocket_api.event_message(msg["id"], payload))

    unsub_plan = data.coordinator.async_add_listener(push)
    unsub_ctrl = data.controller.async_add_listener(push)

    @callback
    def unsubscribe() -> None:
        unsub_plan()
        unsub_ctrl()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    push()
