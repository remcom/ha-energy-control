"""PV inverter curtailment (pure helpers)."""

from __future__ import annotations

TARGET_IMPORT_W = 50.0  # aim for a small import so we never export at a negative price


def curtail_limit_w(
    *,
    max_w: float,
    pv_power_w: float | None,
    grid_power_w: float | None,
    fallback_w: float,
) -> float:
    """PV limit (W) that keeps grid export at ~zero.

    With live PV and grid measurements this is a feedback step: produce what the
    house uses right now (grid import positive). Otherwise use the planned
    fallback (load + charge power).
    """
    if pv_power_w is not None and grid_power_w is not None:
        limit = pv_power_w + grid_power_w - TARGET_IMPORT_W
    else:
        limit = fallback_w
    return max(0.0, min(max_w, limit))


def to_entity_value(limit_w: float, unit: str, max_w: float) -> float:
    """Convert W to the limiter entity's unit."""
    if unit == "%":
        return round(100.0 * limit_w / max_w, 1) if max_w else 0.0
    return round(limit_w)


def from_entity_value(value: float, unit: str, max_w: float) -> float:
    """Convert the limiter entity's value to W."""
    if unit == "%":
        return value * max_w / 100.0
    return value
