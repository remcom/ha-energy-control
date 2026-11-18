"""Measurement coverage, missing data and forecast-quality regressions."""

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.energy_control.history import History
from custom_components.energy_control.optimizer import Action, SlotPlan


def plan(now):
    return SlotPlan(now, 0.25, Action.SELF_CONSUME, 0, 1, 0.3, 0.4, 0, 50, 50, 0.3, 0.05, 0.075)


def sample(history, now, planned, **kwargs):
    return history.sample(
        now,
        mode="auto",
        planned=planned,
        grid_w=kwargs.pop("grid_w", 1000),
        battery_w=0,
        soc_pct=50,
        curtailing=False,
        **kwargs,
    )


async def test_partial_measurement_is_not_full_slot_cost(hass, freezer):
    freezer.move_to("2026-11-15T08:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "coverage")
    p = plan(now)
    sample(h, now, p)
    sample(h, now + timedelta(seconds=10), p)
    closed = sample(h, now + timedelta(minutes=15), p)
    assert closed["sampled_seconds"] == 10
    assert closed["coverage"] == pytest.approx(10 / 900, abs=0.0001)
    assert closed["observed_import_kwh"] == pytest.approx(1 / 360, abs=1e-6)
    assert closed["actual_cost"] == pytest.approx(0.3 / 3600 * 10, abs=1e-6)
    assert closed["extrapolated_cost"] == pytest.approx(0.075)
    freezer.move_to(now + timedelta(minutes=15))
    assert h.actual_cost_since(now) is None
    assert h.observed_cost_since(now) > 0


async def test_missing_grid_is_unknown_not_zero_and_persists(hass, freezer):
    freezer.move_to("2026-11-15T08:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "missing")
    p = plan(now)
    sample(h, now, p, grid_w=None)
    closed = sample(h, now + timedelta(minutes=15), p, grid_w=None)
    assert closed["actual_cost"] is None
    assert closed["actual_grid_kw"] is None
    assert closed["observed_import_kwh"] is None
    assert closed["coverage"] == 0
    assert h.observed_cost_since(now) is None
    await h._store.async_save(h.records)
    await hass.async_block_till_done()
    loaded = History(hass, "missing")
    await loaded.async_load()
    assert loaded.records[0]["actual_cost"] is None


async def test_full_coverage_measures_load_pv_and_daily_cost(hass, freezer):
    freezer.move_to("2026-11-15T08:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "complete")
    p = plan(now)
    for seconds in range(0, 901, 10):
        closed = sample(h, now + timedelta(seconds=seconds), p, load_w=800, pv_w=100)
    assert closed["coverage"] == 1
    assert closed["load_error_kw"] == pytest.approx(0.4)
    assert closed["pv_error_kw"] == pytest.approx(-0.2)
    freezer.move_to(now + timedelta(minutes=15))
    assert h.actual_cost_since(now) == pytest.approx(0.075)
    # A wholly absent quarter makes the total unavailable, even if recorded rows were full.
    freezer.tick(timedelta(minutes=15))
    assert h.actual_cost_since(now) is None
    assert h.coverage_since(now) == 0.5


async def test_cumulative_meters_preferred_and_reset_not_zero(hass, freezer):
    freezer.move_to("2026-11-15T08:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "meters")
    p = plan(now)
    for seconds in range(0, 901, 10):
        closed = sample(
            h,
            now + timedelta(seconds=seconds),
            p,
            grid_w=99999,
            meters_configured=True,
            import_kwh=100 + seconds / 3600,
            export_kwh=30,
        )
    assert closed["measurement_method"] == "cumulative_meters"
    assert closed["observed_import_kwh"] == pytest.approx(0.25)
    assert closed["actual_cost"] == pytest.approx(0.075)
    sample(h, now + timedelta(seconds=910), p, meters_configured=True, import_kwh=0, export_kwh=0)
    # A reset interval is unknown, not an apparent negative energy/saving.
    assert h._acc.grid_samples == 0


async def test_correction_requires_enough_coverage_and_is_bounded(hass, freezer):
    freezer.move_to("2026-11-15T08:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "quality")
    h.records = [
        {
            "start": (now - timedelta(minutes=15 * i)).isoformat(),
            "load_coverage": 1,
            "actual_load_kw": 5,
            "planned_load_kw": 0.4,
        }
        for i in range(1, 21)
    ]
    assert h.recent_load_factor(now) == 1.2
    h.records.pop()
    assert h.recent_load_factor(now) == 1.0
    h.records *= 2
    for row in h.records:
        row["load_coverage"] = 0.5
    assert h.recent_load_factor(now) == 1.0


async def test_corrections_use_uncorrected_forecast(hass, freezer):
    """A correction applied to the plan must not cancel its own measurement."""
    freezer.move_to("2026-11-15T08:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "feedback")
    h.records = [
        {
            "start": (now - timedelta(minutes=15 * i)).isoformat(),
            "load_coverage": 1,
            "actual_load_kw": 0.6,
            "planned_load_kw": 0.6,  # already corrected upward
            "planned_load_forecast_kw": 0.5,
        }
        for i in range(1, 21)
    ]
    assert h.recent_load_factor(now) == pytest.approx(1.2)


async def test_pv_factor_last_hour_skips_curtailed_and_small(hass, freezer):
    freezer.move_to("2026-11-15T12:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "pv")

    def row(i, actual, forecast, **extra):
        return {
            "start": (now - timedelta(minutes=15 * i)).isoformat(),
            "pv_coverage": 1,
            "actual_pv_kw": actual,
            "planned_pv_forecast_kw": forecast,
            **extra,
        }

    h.records = [row(i, 1.0, 2.0) for i in range(1, 5)]
    assert h.recent_pv_factor(now) == pytest.approx(0.5)
    h.records = [row(i, 1.0, 2.0, curtailed=True) for i in range(1, 5)]
    assert h.recent_pv_factor(now) == 1.0
    h.records = [row(i, 0.01, 0.05) for i in range(1, 5)]  # dawn: too small to judge
    assert h.recent_pv_factor(now) == 1.0
    h.records = [row(i, 0.1, 2.0) for i in range(1, 5)]
    assert h.recent_pv_factor(now) == 0.3  # bounded


async def test_forecast_accuracy_energy_weighted(hass, freezer):
    freezer.move_to("2026-11-15T12:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "acc")
    h.records = [
        {
            "start": (now - timedelta(minutes=15 * i)).isoformat(),
            "pv_coverage": 1,
            "actual_pv_kw": 1.0,
            "planned_pv_forecast_kw": 1.25 if i % 2 else 0.75,
            "load_coverage": 1,
            "actual_load_kw": 0.5,
            "planned_load_forecast_kw": 0.5,
        }
        for i in range(1, 11)
    ]
    pv = h.forecast_accuracy(now, "pv")
    assert pv["accuracy"] == 75.0
    assert pv["bias"] == 1.0
    assert h.forecast_accuracy(now, "load")["accuracy"] == 100.0
    h.records = h.records[:5]
    assert h.forecast_accuracy(now, "pv") is None  # too few quarters


def test_energy_flows_split():
    from custom_components.energy_control.history import energy_flows

    f = energy_flows(load_kw=1.0, pv_kw=3.0, battery_kw=1.5)  # sunny: PV to all
    assert (f["pv_to_house"], f["pv_to_battery"], f["pv_to_grid"]) == (1.0, 1.5, 0.5)
    assert f["grid_to_house"] == 0 and f["grid_to_battery"] == 0
    f = energy_flows(load_kw=1.0, pv_kw=0.0, battery_kw=-3.0)  # selling at the peak
    assert (f["battery_to_house"], f["battery_to_grid"]) == (1.0, 2.0)
    f = energy_flows(load_kw=0.5, pv_kw=0.0, battery_kw=2.0)  # grid charging
    assert (f["grid_to_house"], f["grid_to_battery"]) == (0.5, 2.0)


async def test_daily_flows_and_costs(hass, freezer):
    freezer.move_to("2026-11-15T12:00:00+00:00")
    now = dt_util.utcnow()
    h = History(hass, "flows")
    h.records = [
        {
            "start": (now - timedelta(minutes=15 * i)).isoformat(),
            "actual_load_kw": 1.0,
            "actual_pv_kw": 2.0,
            "actual_battery_kw": 0.5,
            "actual_cost": -0.05,
            "planned_cost": -0.04,
            "import_price": 0.30,
            "export_price": 0.20,
        }
        for i in range(1, 5)
    ]
    day = h.daily_flows(now)[-1]
    assert day["house_kwh"] == 1.0 and day["pv_kwh"] == 2.0
    assert day["pv_to_grid"] == 0.5 and day["self_sufficiency"] == 1.0
    assert day["self_consumption"] == 0.75
    costs = h.costs_since(now - timedelta(hours=2))
    assert costs[0]["baseline"] == pytest.approx(-0.25 * 0.20)
    assert costs[0]["actual"] == -0.05
