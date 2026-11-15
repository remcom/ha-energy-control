"""Pure control mapping tests."""

from __future__ import annotations

from custom_components.energy_control.control.base import Limits, Originals, desired_for
from custom_components.energy_control.control.pv_limiter import (
    curtail_limit_w,
    from_entity_value,
    to_entity_value,
)
from custom_components.energy_control.optimizer import Action

LIMITS = Limits(idle_setpoint=30, max_import=8000, max_export=4600, max_charge=3000, max_discharge=2750)
ORIG = Originals(charge_limit=-1, discharge_limit=2750, feed_in_limit=-1)


def test_self_consume_restores_originals():
    d = desired_for(Action.SELF_CONSUME, battery_kw=0.5, grid_kw=0, curtail=False, originals=ORIG, limits=LIMITS)
    assert (d.setpoint, d.charge_limit, d.discharge_limit, d.feed_in_limit) == (30, -1, 2750, -1)


def test_charge_grid():
    d = desired_for(Action.CHARGE_GRID, battery_kw=2.04, grid_kw=2.5, curtail=False, originals=ORIG, limits=LIMITS)
    assert d.setpoint == 8000
    assert d.charge_limit == 2040
    assert d.discharge_limit == 2750


def test_charge_grid_capped():
    d = desired_for(Action.CHARGE_GRID, battery_kw=9, grid_kw=9, curtail=False, originals=ORIG, limits=LIMITS)
    assert d.charge_limit == 3000


def test_discharge_grid():
    d = desired_for(Action.DISCHARGE_GRID, battery_kw=-2.0, grid_kw=-1.5, curtail=False, originals=ORIG, limits=LIMITS)
    assert d.setpoint == -4600
    assert d.discharge_limit == 2000


def test_hold_blocks_discharge_only():
    d = desired_for(Action.HOLD, battery_kw=0, grid_kw=0.4, curtail=False, originals=ORIG, limits=LIMITS)
    assert d.discharge_limit == 0
    assert d.charge_limit == -1


def test_hold_with_export_blocks_charging():
    d = desired_for(Action.HOLD, battery_kw=0, grid_kw=-1.0, curtail=False, originals=ORIG, limits=LIMITS)
    assert d.charge_limit == 0


def test_curtail_sets_zero_feed_in():
    d = desired_for(Action.SELF_CONSUME, battery_kw=0, grid_kw=0, curtail=True, originals=ORIG, limits=LIMITS)
    assert d.feed_in_limit == 0
    assert d.curtail


def test_pv_limit_feedback():
    # Exporting 400 W while PV makes 1500 W -> limit to ~1050 W.
    assert curtail_limit_w(max_w=2700, pv_power_w=1500, grid_power_w=-400, fallback_w=0) == 1050
    # Importing 300 W with PV at 0 -> allow 250 W.
    assert curtail_limit_w(max_w=2700, pv_power_w=0, grid_power_w=300, fallback_w=0) == 250
    # No sensors: planned fallback, clamped.
    assert curtail_limit_w(max_w=2700, pv_power_w=None, grid_power_w=None, fallback_w=5000) == 2700


def test_pv_limit_units():
    assert to_entity_value(1350, "percent", 2700) == 50.0
    assert from_entity_value(50, "percent", 2700) == 1350
    assert to_entity_value(1350.4, "watt", 2700) == 1350
