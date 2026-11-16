"""Data model for the optimizer. Pure Python: no Home Assistant imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    """What the battery system should do during a slot."""

    SELF_CONSUME = "self_consume"  # ESS default: PV surplus -> battery, battery -> load
    HOLD = "hold"  # keep SoC: no discharge (grid covers load)
    CHARGE_GRID = "charge_grid"  # charge the battery (partly) from the grid
    DISCHARGE_GRID = "discharge_grid"  # discharge the battery into the grid


@dataclass(frozen=True, slots=True)
class SlotInput:
    """Forecast for one time slot. Energies in kWh, prices in EUR/kWh."""

    start: datetime
    hours: float  # slot length in hours (first slot may be partial)
    import_price: float
    export_price: float
    pv_kwh: float
    load_kwh: float


@dataclass(frozen=True, slots=True)
class BatteryParams:
    """Battery and inverter capabilities."""

    capacity_kwh: float
    soc_kwh: float  # current stored energy
    min_soc_kwh: float
    max_soc_kwh: float
    max_charge_kw: float  # battery side (DC) charge power limit
    max_discharge_kw: float  # battery side (DC) discharge power limit
    charge_efficiency: float = 0.95  # AC -> stored
    discharge_efficiency: float = 0.95  # stored -> AC
    cycle_cost: float = 0.02  # EUR per kWh discharged (battery wear)


@dataclass(frozen=True, slots=True)
class GridParams:
    """Grid connection limits in kW (AC)."""

    max_import_kw: float = 8.0
    max_export_kw: float = 8.0


@dataclass(frozen=True, slots=True)
class Strategy:
    """What the optimizer may do. Toggled by the user."""

    grid_charge: bool = True
    battery_to_grid: bool = False
    curtail_negative: bool = True
    # Extra hurdle (EUR/kWh) on energy charged from the grid and on battery
    # energy exported: arbitrage only happens when the spread clearly pays.
    arbitrage_margin: float = 0.01


@dataclass(slots=True)
class SlotPlan:
    """Optimizer decision for one slot."""

    start: datetime
    hours: float
    action: Action
    battery_kw: float  # + charge / - discharge (battery side, DC)
    grid_kw: float  # + import / - export (AC)
    pv_kw: float  # forecast PV
    load_kw: float  # forecast load
    curtail_kw: float  # PV deliberately not produced
    soc_start_pct: float
    soc_end_pct: float
    import_price: float
    export_price: float
    cost: float  # EUR paid for grid energy in this slot (excl. wear and hurdles)
    wear_cost: float = 0.0  # EUR battery wear in this slot
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-friendly dict."""
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["action"] = str(self.action)
        for key, value in data.items():
            if isinstance(value, float):
                data[key] = round(value, 4)
        return data


@dataclass(slots=True)
class Plan:
    """Full optimized schedule."""

    created: datetime
    slots: list[SlotPlan] = field(default_factory=list)
    total_cost: float = 0.0  # EUR paid for grid energy over horizon, with battery
    wear_cost: float = 0.0  # EUR battery wear over horizon (not in total_cost)
    baseline_cost: float = 0.0  # EUR over horizon, no battery, no curtailment
    terminal_value: float = 0.0  # EUR/kWh value given to energy left at horizon end
    soc_start_kwh: float = 0.0
    soc_end_kwh: float = 0.0

    @property
    def savings(self) -> float:
        """Savings vs. no battery, net of wear, correcting for change in stored energy.

        The arbitrage hurdle only steers the optimizer; it is never paid and
        therefore not part of any reported cost.
        """
        stored_delta = (self.soc_end_kwh - self.soc_start_kwh) * self.terminal_value
        return self.baseline_cost - self.total_cost - self.wear_cost + stored_delta

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-friendly dict."""
        return {
            "created": self.created.isoformat(),
            "total_cost": round(self.total_cost, 4),
            "baseline_cost": round(self.baseline_cost, 4),
            "wear_cost": round(self.wear_cost, 4),
            "savings": round(self.savings, 4),
            "terminal_value": round(self.terminal_value, 4),
            "soc_start_kwh": round(self.soc_start_kwh, 3),
            "soc_end_kwh": round(self.soc_end_kwh, 3),
            "slots": [slot.as_dict() for slot in self.slots],
        }

    def slot_at(self, when: datetime) -> SlotPlan | None:
        """Return the slot covering `when`."""
        for slot in self.slots:
            end = slot.start.timestamp() + slot.hours * 3600
            if slot.start.timestamp() <= when.timestamp() < end:
                return slot
        return None
