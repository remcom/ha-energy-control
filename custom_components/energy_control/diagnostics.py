"""Diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .data import EnergyControlConfigEntry
from .websocket import build_payload


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EnergyControlConfigEntry) -> dict[str, Any]:
    """Config, settings, last plan and recent history."""
    data = entry.runtime_data
    return {
        "config": data.coordinator.config,
        "load_profile": (
            {f"{'we' if k[0] else 'wd'}_{k[1]:02d}": round(v, 3) for k, v in data.coordinator.load_profile.kw.items()}
            if data.coordinator.load_profile
            else None
        ),
        "price_points": len(data.coordinator.prices),
        "pv_intervals": len(data.coordinator.pv),
        "payload": build_payload(data, 48),
    }
