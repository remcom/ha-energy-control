"""Live telemetry validation, using reports rather than value changes."""

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .util import parse_dt, to_float

MAX_TELEMETRY_AGE = timedelta(minutes=5)


def soc_reading(
    hass: HomeAssistant, entity_id: str | None, now: datetime | None = None
) -> tuple[float | None, str | None]:
    """Return valid SoC and a specific failure reason.

    last_reported advances even for unchanged states. Sources may supply an
    explicit source timestamp (ISO 8601) via `source_timestamp` instead.
    """
    state = hass.states.get(entity_id) if entity_id else None
    if state is None:
        return None, "no_soc"
    value = to_float(state.state)
    if value is None:
        return None, "no_soc"
    if not 0 <= value <= 100 or state.attributes.get("unit_of_measurement") not in (None, "%"):
        return None, "invalid_soc"
    if reason := report_error(state, now):
        return None, f"{reason}_soc"
    return value, None


def report_error(state: State, now: datetime | None = None) -> str | None:
    """Validate the report timestamp, independently of value changes."""
    reported = state.last_reported
    if "source_timestamp" in state.attributes:
        reported = parse_dt(state.attributes["source_timestamp"])
        if reported is None:
            return "invalid_timestamp"
    age = (now or dt_util.utcnow()) - reported
    if age > MAX_TELEMETRY_AGE or age < -timedelta(minutes=1):
        return "stale"
    return None
