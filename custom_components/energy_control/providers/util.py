"""Shared helpers for providers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def to_float(value: Any) -> float | None:
    """Float or None for missing / non-numeric values."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value: Any) -> datetime | None:
    """Parse an ISO string or datetime to an aware datetime (naive -> UTC)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
