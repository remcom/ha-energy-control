"""Planned-vs-actual history per slot, persisted for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, HISTORY_DAYS
from .optimizer import SlotPlan
from .providers.slots import SLOT_MINUTES, floor_slot


@dataclass(slots=True)
class _Accumulator:
    start: datetime
    planned: dict[str, Any] | None
    mode: str
    seconds: float = 0.0
    grid_ws: float = 0.0
    import_ws: float = 0.0
    export_ws: float = 0.0
    battery_ws: float = 0.0
    grid_samples: float = 0.0
    battery_samples: float = 0.0
    soc_end: float | None = None
    curtailed: bool = False
    last: datetime | None = None


class History:
    """Rolling per-slot record of what was planned and what happened."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Init."""
        self._store: Store[list[dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.history")
        self.records: list[dict[str, Any]] = []
        self._acc: _Accumulator | None = None

    async def async_load(self) -> None:
        """Load from disk."""
        self.records = await self._store.async_load() or []

    def sample(
        self,
        now: datetime,
        *,
        mode: str,
        planned: SlotPlan | None,
        grid_w: float | None,
        battery_w: float | None,
        soc_pct: float | None,
        curtailing: bool,
    ) -> dict[str, Any] | None:
        """Add a measurement; returns the closed record when a slot ends."""
        closed = None
        slot_start = floor_slot(now)
        if self._acc is not None and self._acc.start != slot_start:
            closed = self._close(self._acc)
            self._acc = None
        if self._acc is None:
            self._acc = _Accumulator(
                start=slot_start,
                planned=_planned_snapshot(planned),
                mode=mode,
            )
        acc = self._acc
        dt_s = (now - acc.last).total_seconds() if acc.last else 0.0
        acc.last = now
        if 0 < dt_s < 120:
            acc.seconds += dt_s
            if grid_w is not None:
                acc.grid_ws += grid_w * dt_s
                acc.import_ws += max(grid_w, 0.0) * dt_s
                acc.export_ws += max(-grid_w, 0.0) * dt_s
                acc.grid_samples += dt_s
            if battery_w is not None:
                acc.battery_ws += battery_w * dt_s
                acc.battery_samples += dt_s
        if soc_pct is not None:
            acc.soc_end = soc_pct
        acc.curtailed = acc.curtailed or curtailing
        return closed

    def _close(self, acc: _Accumulator) -> dict[str, Any]:
        planned = acc.planned or {}
        grid_kw = acc.grid_ws / acc.grid_samples / 1000 if acc.grid_samples else None
        battery_kw = acc.battery_ws / acc.battery_samples / 1000 if acc.battery_samples else None
        hours = SLOT_MINUTES / 60
        actual_cost = None
        if acc.grid_samples and planned:
            # Import and export are priced separately: a slot can contain both.
            # Average power over the sampled time, extrapolated to the full slot.
            import_kwh = acc.import_ws / acc.grid_samples / 1000 * hours
            export_kwh = acc.export_ws / acc.grid_samples / 1000 * hours
            actual_cost = import_kwh * planned["import_price"] - export_kwh * planned["export_price"]
        record = {
            "start": acc.start.isoformat(),
            "mode": acc.mode,
            **{f"planned_{k}": v for k, v in planned.items() if k not in ("import_price", "export_price")},
            "import_price": planned.get("import_price"),
            "export_price": planned.get("export_price"),
            "actual_grid_kw": _round(grid_kw),
            "actual_battery_kw": _round(battery_kw),
            "actual_soc": _round(acc.soc_end),
            "actual_cost": _round(actual_cost),
            "curtailed": acc.curtailed,
        }
        self.records.append(record)
        cutoff = (dt_util.utcnow() - timedelta(days=HISTORY_DAYS)).isoformat()
        self.records = [r for r in self.records if r["start"] >= cutoff]
        self._store.async_delay_save(lambda: self.records, 30)
        return record

    def since(self, start: datetime) -> list[dict[str, Any]]:
        """Records from `start` onwards."""
        result = []
        for record in self.records:
            when = dt_util.parse_datetime(record["start"])
            if when is not None and when >= start:
                result.append(record)
        return result

    def actual_cost_since(self, start: datetime) -> float:
        """Realised cost (EUR) of closed slots since `start`."""
        return sum(r["actual_cost"] or 0.0 for r in self.since(start))

    def planned_cost_since(self, start: datetime) -> float:
        """Planned cost (EUR) of closed slots since `start`."""
        return sum(r.get("planned_cost") or 0.0 for r in self.since(start))


def _planned_snapshot(slot: SlotPlan | None) -> dict[str, Any] | None:
    if slot is None:
        return None
    return {
        "action": str(slot.action),
        "battery_kw": round(slot.battery_kw, 3),
        "grid_kw": round(slot.grid_kw, 3),
        "soc": round(slot.soc_end_pct, 2),
        "cost": round(slot.cost / slot.hours * SLOT_MINUTES / 60, 5) if slot.hours else 0.0,
        "import_price": round(slot.import_price, 5),
        "export_price": round(slot.export_price, 5),
    }


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)
