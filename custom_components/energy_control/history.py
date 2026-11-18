"""Planned-vs-actual history per slot, persisted for the dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
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
    load_ws: float = 0.0
    pv_ws: float = 0.0
    load_samples: float = 0.0
    pv_samples: float = 0.0
    hp_ws: float = 0.0
    hp_samples: float = 0.0
    meter_based: bool = False
    last: datetime | None = None


class History:
    """Rolling per-slot record of what was planned and what happened."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Init."""
        self._store: Store[list[dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.history")
        self.records: list[dict[str, Any]] = []
        self._acc: _Accumulator | None = None
        self._previous: tuple[float | None, ...] = (None, None, None, None, None)
        self._previous_meters: tuple[float | None, float | None] = (None, None)
        self._last_sample: datetime | None = None

    async def async_load(self) -> None:
        """Load from disk."""
        self.records = await self._store.async_load() or []
        for record in self.records:
            if "sampled_seconds" not in record:
                record["extrapolated_cost"] = record.get("actual_cost")
                record["actual_cost"] = None
                record["coverage"] = 0.0
                record["measurement_method"] = "legacy_extrapolated"

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
        load_w: float | None = None,
        pv_w: float | None = None,
        import_kwh: float | None = None,
        export_kwh: float | None = None,
        meters_configured: bool = False,
        hp_w: float | None = None,
    ) -> dict[str, Any] | None:
        """Add a measurement; returns the closed record when a slot ends."""
        closed = None
        # Keep malformed measurements out of accounting, even for direct callers.
        grid_w, battery_w, load_w, pv_w, import_kwh, export_kwh, hp_w = (
            value if value is not None and isfinite(value) else None
            for value in (grid_w, battery_w, load_w, pv_w, import_kwh, export_kwh, hp_w)
        )
        meter_w = None
        elapsed = (now - self._last_sample).total_seconds() if self._last_sample else 0
        if meters_configured and 0 < elapsed < 120:
            old_import, old_export = self._previous_meters
            if (
                old_import is not None
                and old_export is not None
                and import_kwh is not None
                and export_kwh is not None
                and import_kwh >= old_import
                and export_kwh >= old_export
            ):
                meter_w = ((import_kwh - old_import) * 3600000 / elapsed, (export_kwh - old_export) * 3600000 / elapsed)
        self._previous_meters = (import_kwh, export_kwh)
        self._last_sample = now
        current = (grid_w, battery_w, load_w, pv_w, hp_w)
        values = tuple(old if new is not None else None for old, new in zip(self._previous, current, strict=True))
        slot_start = floor_slot(now)
        continuous_boundary = False
        if self._acc is not None and self._acc.start != slot_start:
            end = self._acc.start + timedelta(minutes=SLOT_MINUTES)
            # Close the observed tail using the last reading, only across a
            # short reporting gap. Long gaps are explicitly unobserved.
            if self._acc.last and 0 < (now - self._acc.last).total_seconds() < 120:
                self._integrate(self._acc, (end - self._acc.last).total_seconds(), values, meters_configured, meter_w)
                continuous_boundary = True
            closed = self._close(self._acc)
            self._acc = None
        if self._acc is None:
            self._acc = _Accumulator(
                start=slot_start,
                planned=_planned_snapshot(planned),
                mode=mode,
                meter_based=meters_configured,
                last=slot_start if continuous_boundary else None,
            )
        acc = self._acc
        dt_s = (now - acc.last).total_seconds() if acc.last else 0.0
        acc.last = now
        if 0 < dt_s < 120:
            # Both endpoints must be available; missing samples are not zero.
            self._integrate(acc, dt_s, values, meters_configured, meter_w)
        self._previous = current
        if soc_pct is not None:
            acc.soc_end = soc_pct
        acc.curtailed = acc.curtailed or curtailing
        return closed

    @staticmethod
    def _integrate(
        acc: _Accumulator,
        seconds: float,
        values: tuple[float | None, ...],
        meters_configured: bool = False,
        meter_w: tuple[float, float] | None = None,
    ) -> None:
        if seconds <= 0:
            return
        grid, battery, load, pv, hp = values
        acc.seconds += seconds
        if meters_configured:
            if meter_w is not None:
                imp, exp = meter_w
                acc.grid_ws += (imp - exp) * seconds
                acc.import_ws += imp * seconds
                acc.export_ws += exp * seconds
                acc.grid_samples += seconds
        elif grid is not None:
            acc.grid_ws += grid * seconds
            acc.import_ws += max(grid, 0.0) * seconds
            acc.export_ws += max(-grid, 0.0) * seconds
            acc.grid_samples += seconds
        if battery is not None:
            acc.battery_ws += battery * seconds
            acc.battery_samples += seconds
        if load is not None:
            acc.load_ws += max(load, 0.0) * seconds
            acc.load_samples += seconds
        if pv is not None:
            acc.pv_ws += max(pv, 0.0) * seconds
            acc.pv_samples += seconds
        if hp is not None:
            acc.hp_ws += max(hp, 0.0) * seconds
            acc.hp_samples += seconds

    def _close(self, acc: _Accumulator) -> dict[str, Any]:
        planned = acc.planned or {}
        grid_kw = acc.grid_ws / acc.grid_samples / 1000 if acc.grid_samples else None
        battery_kw = acc.battery_ws / acc.battery_samples / 1000 if acc.battery_samples else None
        actual_cost = None
        if acc.grid_samples and planned and planned.get("export_price_known", True):
            # Import and export are priced separately: a slot can contain both.
            # Only measured energy contributes; extrapolation is a separate field.
            import_kwh = acc.import_ws / 3600000
            export_kwh = acc.export_ws / 3600000
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
            "actual_cost": _round(actual_cost, 6),  # observed energy only
            "measurement_method": "cumulative_meters" if acc.meter_based else "power_samples",
            "sampled_seconds": acc.grid_samples,
            "coverage": round(acc.grid_samples / (SLOT_MINUTES * 60), 4),
            "observed_import_kwh": _round(acc.import_ws / 3600000 if acc.grid_samples else None, 6),
            "observed_export_kwh": _round(acc.export_ws / 3600000 if acc.grid_samples else None, 6),
            "extrapolated_cost": _round(
                actual_cost * SLOT_MINUTES * 60 / acc.grid_samples if actual_cost is not None else None, 6
            ),
            "actual_load_kw": _round(acc.load_ws / acc.load_samples / 1000 if acc.load_samples else None),
            "actual_pv_kw": _round(acc.pv_ws / acc.pv_samples / 1000 if acc.pv_samples else None),
            "actual_hp_kw": _round(acc.hp_ws / acc.hp_samples / 1000 if acc.hp_samples >= 450 else None),
            "load_coverage": round(acc.load_samples / (SLOT_MINUTES * 60), 4),
            "pv_coverage": round(acc.pv_samples / (SLOT_MINUTES * 60), 4),
            "load_error_kw": _round(
                acc.load_ws / acc.load_samples / 1000 - planned["load_kw"]
                if acc.load_samples and "load_kw" in planned
                else None
            ),
            "pv_error_kw": _round(
                acc.pv_ws / acc.pv_samples / 1000 - planned["pv_kw"] if acc.pv_samples and "pv_kw" in planned else None
            ),
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

    def coverage_since(self, start: datetime) -> float:
        """Fraction of elapsed closed quarters actually measured."""
        end = floor_slot(dt_util.utcnow())
        expected = max(0.0, (end - start).total_seconds())
        return min(1.0, sum(r.get("sampled_seconds", 0) for r in self.since(start)) / expected) if expected else 0.0

    def observed_cost_since(self, start: datetime) -> float | None:
        """Cost of measured energy only; None when nothing was priced."""
        values = [r["actual_cost"] for r in self.since(start) if r.get("actual_cost") is not None]
        return sum(values) if values else None

    def actual_cost_since(self, start: datetime) -> float | None:
        """Daily total only when every elapsed closed interval has coverage."""
        records = self.since(start)
        expected = int(max(0, (floor_slot(dt_util.utcnow()) - start).total_seconds()) / (SLOT_MINUTES * 60))
        if (
            not records
            or len({r["start"] for r in records}) != expected
            or any(r.get("actual_cost") is None or r.get("coverage", 0) < 0.98 for r in records)
        ):
            return None
        return self.observed_cost_since(start)

    def recent_load_factor(self, now: datetime) -> float:
        """Bounded load correction after at least 20 well-observed quarters (12 h)."""
        ratio = self._ratio(now - timedelta(hours=12), "load", min_rows=20, min_kw=0.05)
        return 1.0 if ratio is None else max(0.8, min(1.2, ratio))

    def recent_pv_factor(self, now: datetime) -> float:
        """Bounded PV correction from the last hour (clouds the forecast missed).

        Needs a meaningful forecast in that hour: dawn, dusk and night readings
        are too small to give a reliable ratio. Curtailed quarters are skipped.
        """
        ratio = self._ratio(now - timedelta(hours=1), "pv", min_rows=3, min_kw=0.1, min_total_kw=0.6)
        return 1.0 if ratio is None else max(0.3, min(1.7, ratio))

    def _ratio(
        self, start: datetime, kind: str, *, min_rows: int, min_kw: float, min_total_kw: float = 0.0
    ) -> float | None:
        """Actual / uncorrected forecast over well-observed quarters since `start`."""
        rows = [
            (r[f"actual_{kind}_kw"], forecast)
            for r in self.since(start)
            if r.get(f"{kind}_coverage", 0) >= 0.95
            and r.get(f"actual_{kind}_kw") is not None
            and not (kind == "pv" and r.get("curtailed"))
            and (forecast := _forecast_kw(r, kind)) is not None
            and forecast > min_kw
        ]
        total = sum(f for _, f in rows)
        if len(rows) < min_rows or total <= min_total_kw:
            return None
        return sum(a for a, _ in rows) / total

    def forecast_accuracy(self, now: datetime, kind: str, days: int = 7) -> dict[str, Any] | None:
        """Accuracy of the uncorrected forecast over the last `days`.

        accuracy = 100 x (1 - sum |actual - forecast| / sum actual), energy
        weighted, so quiet hours do not dominate; bias = actual / forecast.
        PV only counts daylight quarters that were not curtailed.
        """
        pairs = [
            (r[f"actual_{kind}_kw"], forecast)
            for r in self.since(now - timedelta(days=days))
            if r.get(f"{kind}_coverage", 0) >= 0.95
            and r.get(f"actual_{kind}_kw") is not None
            and (forecast := _forecast_kw(r, kind)) is not None
            and not (kind == "pv" and (r.get("curtailed") or max(forecast, r[f"actual_{kind}_kw"]) < 0.05))
        ]
        actual = sum(a for a, _ in pairs)
        forecast = sum(f for _, f in pairs)
        if len(pairs) < 8 or actual <= 0:
            return None
        error = sum(abs(a - f) for a, f in pairs)
        return {
            "accuracy": round(max(0.0, 100 * (1 - error / actual)), 1),
            "bias": round(actual / forecast, 3) if forecast > 0 else None,
            "quarters": len(pairs),
            "actual_kwh": round(actual / 4, 2),
            "forecast_kwh": round(forecast / 4, 2),
        }

    def live_average(self, now: datetime) -> tuple[float | None, float | None]:
        """Average (load kW, PV kW) of the running slot after at least 60 s."""
        acc = self._acc
        if acc is None or acc.start != floor_slot(now):
            return None, None
        load = acc.load_ws / acc.load_samples / 1000 if acc.load_samples >= 60 else None
        pv = acc.pv_ws / acc.pv_samples / 1000 if acc.pv_samples >= 60 else None
        return load, pv

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
        "load_kw": round(slot.load_kw, 3),
        "pv_kw": round(slot.pv_kw, 3),
        "hp_kw": round(slot.hp_kw, 3),
        "ev_kw": round(slot.ev_kw, 3),
        "pv_forecast_kw": None if slot.pv_forecast_kw is None else round(slot.pv_forecast_kw, 3),
        "load_forecast_kw": None if slot.load_forecast_kw is None else round(slot.load_forecast_kw, 3),
        "export_price_known": slot.export_price_known,
        "soc": round(slot.soc_end_pct, 2),
        "cost": round(slot.cost / slot.hours * SLOT_MINUTES / 60, 5) if slot.hours else 0.0,
        "import_price": round(slot.import_price, 5),
        "export_price": round(slot.export_price, 5),
    }


def _forecast_kw(record: dict[str, Any], kind: str) -> float | None:
    """Uncorrected forecast of a record; older records only have the corrected one."""
    value = record.get(f"planned_{kind}_forecast_kw")
    return value if value is not None else record.get(f"planned_{kind}_kw")


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)
