"""Runtime data stored on the config entry."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .control.victron import VictronController
from .coordinator import EnergyControlCoordinator
from .history import History


@dataclass(slots=True)
class EnergyControlData:
    """Everything one config entry owns."""

    coordinator: EnergyControlCoordinator
    controller: VictronController
    history: History


type EnergyControlConfigEntry = ConfigEntry[EnergyControlData]
