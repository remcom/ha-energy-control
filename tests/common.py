"""Shared test constants and helpers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

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


def written(calls) -> dict[str, float]:
    """Last written value per entity."""
    return {c.data["entity_id"]: c.data["value"] for c in calls}


def accept(hass: HomeAssistant, calls) -> None:
    """Simulate the hardware accepting the written values."""
    for entity_id, value in written(calls).items():
        hass.states.async_set(entity_id, str(value), hass.states.get(entity_id).attributes)
