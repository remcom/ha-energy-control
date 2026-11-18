"""Pure mapping from an action to actuator values (no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass

from ..optimizer.model import Action

UNLIMITED = -1.0  # Victron convention for "no limit"
# Planned grid power below this counts as "battery balances the house".
BALANCED_GRID_KW = 0.1


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
    discharge_efficiency: float = 1.0  # battery (DC) -> AC


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
      The plan's battery power is DC; the inverter limit is AC, so it is scaled
      by the discharge efficiency.
    - HOLD: inverter limit 0 so the grid covers the load; charging is blocked only
      when the plan deliberately exports PV instead of storing it.
    - SELF_CONSUME: original ESS behaviour around the idle setpoint, so the
      battery absorbs forecast errors (unexpected PV surplus, a load spike).
      Only a deliberate grid trade in the plan caps one direction: storing
      part of the surplus while exporting the rest caps charging (and blocks
      discharge); covering part of the load while importing the rest caps
      discharge (unexpected surplus may still be stored).
    """
    setpoint = limits.idle_setpoint
    charge = originals.charge_limit
    discharge = originals.discharge_limit
    feed_in = originals.feed_in_limit

    if action == Action.CHARGE_GRID:
        setpoint = limits.max_import
        charge = min(limits.max_charge, max(0.0, battery_kw * 1000))
    elif action == Action.DISCHARGE_GRID:
        setpoint = -limits.max_export
        ac_w = -battery_kw * 1000 * limits.discharge_efficiency
        discharge = min(limits.max_discharge, max(0.0, ac_w))
    elif action == Action.SELF_CONSUME and abs(grid_kw) >= BALANCED_GRID_KW:
        if battery_kw > 1e-3 and grid_kw < 0:
            charge = min(limits.max_charge, battery_kw * 1000)
            discharge = 0.0
        elif battery_kw < -1e-3 and grid_kw > 0:
            discharge = min(limits.max_discharge, -battery_kw * 1000 * limits.discharge_efficiency)
    elif action == Action.HOLD:
        discharge = 0.0
        if grid_kw < 0 and abs(battery_kw) < 1e-3:
            charge = 0.0

    if curtail:
        feed_in = 0.0
    return Desired(action, setpoint, charge, discharge, feed_in, curtail)
