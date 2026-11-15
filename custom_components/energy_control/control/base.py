"""Pure mapping from an action to actuator values (no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass

from ..optimizer.model import Action

UNLIMITED = -1.0  # Victron convention for "no limit"


@dataclass(frozen=True, slots=True)
class Originals:
    """Actuator values before Energy Control took over (W)."""

    charge_limit: float = UNLIMITED
    discharge_limit: float = UNLIMITED
    feed_in_limit: float = UNLIMITED
    pv_limit_w: float | None = None


@dataclass(frozen=True, slots=True)
class Limits:
    """Static limits from config (W)."""

    idle_setpoint: float
    max_import: float
    max_export: float
    max_charge: float
    max_discharge: float


@dataclass(frozen=True, slots=True)
class Desired:
    """Target actuator values (W). None = do not touch."""

    action: Action
    setpoint: float
    charge_limit: float
    discharge_limit: float
    feed_in_limit: float
    curtail: bool


def desired_for(
    action: Action,
    *,
    battery_kw: float,
    grid_kw: float,
    curtail: bool,
    originals: Originals,
    limits: Limits,
) -> Desired:
    """Translate an action into ESS settings.

    - CHARGE_GRID: high grid setpoint, charging capped by the charge limit, so the
      ESS charges at the planned rate whatever the load does.
    - DISCHARGE_GRID: negative setpoint, discharge capped by the inverter limit.
    - HOLD: inverter limit 0 so the grid covers the load; charging is blocked only
      when the plan deliberately exports PV instead of storing it.
    - SELF_CONSUME: original ESS behaviour around the idle setpoint.
    """
    setpoint = limits.idle_setpoint
    charge = originals.charge_limit
    discharge = originals.discharge_limit
    feed_in = originals.feed_in_limit

    if action == Action.CHARGE_GRID:
        setpoint = limits.max_import
        charge = min(limits.max_charge, max(100.0, round(battery_kw * 1000, -1)))
    elif action == Action.DISCHARGE_GRID:
        setpoint = -limits.max_export
        discharge = min(limits.max_discharge, max(100.0, round(-battery_kw * 1000, -1)))
    elif action == Action.HOLD:
        discharge = 0.0
        if grid_kw < 0 and abs(battery_kw) < 1e-3:
            charge = 0.0

    if curtail:
        feed_in = 0.0
    return Desired(action, setpoint, charge, discharge, feed_in, curtail)
