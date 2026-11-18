# Energy Control

> [!WARNING]
> **Experimental — in early development. Not recommended for use.**
> Energy Control directly controls your battery inverter, and optionally your heat pump and EV
> charger. It is new, changes quickly between releases and has only been tested on a single
> installation. Bugs can cost money, empty your battery at the wrong moment or leave a device in
> an unexpected state. Use it only if you understand the risks, start in `advisory` mode, and keep
> an eye on what it does. No warranty (see [LICENSE](LICENSE)).

A local, cost-optimizing battery scheduler for Home Assistant — a self-hosted alternative to
Victron VRM Dynamic ESS. Every 5 minutes it plans the next 24–48 h from dynamic prices, solar
forecast and your own consumption history, executes the plan on a Victron ESS and can curtail an
AC-coupled PV inverter when export prices go negative.

## Features

- **Optimizer** — dynamic programming over battery state of charge per 15‑minute slot, replanned
  every 5 minutes. Accounts for charge/discharge efficiency, battery wear (€/kWh) and an arbitrage
  margin, so grid charging and battery→grid export only happen when the spread actually pays.
- **Strategies (switches)** — grid charging · battery to grid (arbitrage) · curtail PV at negative
  prices · plan with estimated prices · heat pump control.
- **Modes** — `off` · `advisory` (plan only, no writes) · `auto` · `self_consumption` · `force_charge` · `force_discharge` · `hold`.
- **Dynamic prices** — native core Nord Pool forecasts, Tibber Prices (15‑min), ENTSO‑e, EnergyZero
  and other sensors that expose a price list in their attributes. All‑in or spot prices (+ markup, energy tax, VAT).
- **Price estimates** — before tomorrow's prices are published the plan still looks 36 h ahead,
  using an external forecast sensor or a built-in model (price history, solar and wind). Estimates
  steer the plan but are never executed.
- **Export compensation** — netting (NL *saldering*, export = import price) with an end date, after
  which it switches to `spot × factor − fee`; or a fixed price.
- **Solar forecast** — Solcast (median/P10/P90, later days added automatically), Forecast.Solar,
  Open‑Meteo Solar Forecast; corrected for clouds from measured production in the last hour.
- **Load forecast** — robust hourly profile (weekday/weekend) from recorder statistics, live values
  for the running quarter, recent-deviation correction; heat pump and EV modelled separately.
- **Heat pump** — SG Ready / EVU: blocks expensive quarters when the catch-up is cheaper, limited by
  outdoor temperature; comfort/boost when energy is cheap. Consumption modelled from outdoor
  temperature and time of day.
- **EV charging** — cheapest quarters before a ready-by time, or now.
- **Cheapest window service** — when to run the dishwasher, washing machine or EV.
- **Forecast accuracy sensors** — PV and consumption, 7 days.
- **Victron control** — via the core Victron GX integration entities (auto-suggested): grid
  setpoint, max charge power, max inverter power, max feed‑in. VRM Dynamic ESS is switched off
  while Energy Control is in charge and restored afterwards.
- **PV curtailment** — feedback limiter on any `number` power-limit entity (W or %), e.g. a Solis
  inverter seen by the GX as a PV inverter.
- **Safe by default** — starts in `advisory`. Original settings are saved before taking over and
  restored when switching to `off`/`advisory`, unloading, or stopping Home Assistant. Stale prices
  or missing/stale SoC pause forced actions. Live SoC limits block the prohibited direction, including
  during manual modes and overrides. DESS must acknowledge off before control starts; failures appear
  in Repairs. The heat pump always returns to normal when control stops.
- **Dashboard card** — bundled (no extra install): one timeline with plan vs actual (estimated
  prices faded, heat pump/EV strips), schedule, history and summary.

## Install

Requires **Home Assistant 2026.10.4 or newer** (Python 3.14).

1. HACS → ⋮ → *Custom repositories* → add this repository as *Integration*.
2. Install **Energy Control**, restart Home Assistant.
3. *Settings → Devices & services → Add integration → Energy Control*.

## Configuration

| Step | What |
|---|---|
| Prices | Forecast sensor or core Nord Pool entry + market area, taxes, export model (netting end date, spot factor/fee, fixed). |
| Solar | One or more forecast sensors (Solcast: select *today*, *tomorrow*, *day 3* sensors). Forecasts of all selected sensors are **added up** (e.g. one per PV array), so don't select two sensors that cover the same array and period. |
| Battery | SoC sensor, usable capacity, max charge/discharge power, efficiencies, battery power sensor. |
| Consumption & grid | Household consumption sensor (without battery), grid power sensor (+ = import), optional cumulative import/export meters (Wh or kWh; select both), grid limits. |
| Victron ESS control | Grid setpoint, charge/inverter/feed-in limits, DESS mode select, PV limiter. Leave the setpoint empty for advisory-only use. |

Everything can be changed later under *Configure*. Runtime tuning is done with entities:
minimum/maximum SoC, battery wear cost (ct/kWh discharged) and arbitrage margin (ct/kWh).

Reported costs (plan cost, planned vs actual cost) are what you pay for grid energy. Battery
wear is reported separately (`wear_cost` in the plan summary) and subtracted from the savings.
The arbitrage margin is only a hurdle for the optimizer: it is never part of a reported cost.

### Prices and the netting transition

In Prices, select either a forecast sensor or the core Nord Pool entry and an explicit area such as
`NL` or `SE3`. Native Nord Pool takes precedence when selected. It requests **EUR**, converts the
[core action's EUR/MWh response](https://www.home-assistant.io/actions/nordpool.get_prices_for_date/)
to EUR/kWh, and preserves the raw spot component. Markup, tax and VAT are then added for import.
Published days are cached; missing tomorrow is retried around 13:00 CET/CEST and every 15 minutes
thereafter. Missing today retries every five minutes. Cache rollover follows the market date and
supports hourly and quarter-hour intervals. No manual template forecast is needed.

For sensor sources, EUR/kWh, EUR/MWh and ct/kWh are supported. Explicit other currencies/units are
rejected; legacy sensors without a unit retain the EUR/kWh convention. NaN/infinity, invalid durations
and negative PV/load data are rejected. A spot-only source must be configured with “Prices include
taxes and fees” disabled. An all-in import price is never silently reused as spot export compensation.

Netting ends at local midnight on the configured date (default `2027-01-01`). A missing raw spot
component raises a warning 60 days before the transition. Configure the actual fixed export contract,
a source with raw energy prices, or an explicitly spot-only source. Unknown export prices block
battery export for those intervals; incomplete plan costs and savings are hidden, with
`export_prices_complete: false` exposed in the API. No supplier-specific tax reversal is assumed.

### Price estimates beyond the published prices

Tomorrow's day-ahead prices are published around 13:00. Before that, a plan would end at midnight
and could not see tomorrow's morning peak, so it would undervalue energy kept in the battery
overnight. The plan therefore always looks 36 hours ahead, filling the gap with **estimated** prices
(switch *Plan with estimated prices*, on by default):

1. **External forecast** (optional *External price forecast sensor*), for example
   [energyforecast.de](https://energyforecast.de) through the EPEX Spot integration. Its values are
   calibrated against the published prices: fitted directly on the overlap, otherwise treated as a
   spot price and converted with your supplier's own spot → all-in relation.
2. **Built-in model**, learned from an archive of published prices (35 days, seeded from the price
   sensor's recorder history on first start):
   `price = typical price for this quarter (weekday/weekend, recency weighted)
   + persistence of the last 24 h's deviation + solar effect + wind effect`.
   Solar uses your own PV forecast as a proxy for national solar output; wind uses the hourly
   forecast of the selected *weather* entity. Coefficients start at sensible priors and are learned
   with ridge regression as the archive grows; estimates are clamped to the observed price range.

Estimates only steer the plan (mainly how much energy to keep for later). They are faded in the
chart (“≈ estimated prices”), excluded from reported plan cost and savings, and **never executed**:
if a published price is still missing when an estimated quarter starts, the controller falls back
to plain self consumption.

### Measured cost and forecast quality

History records sampled seconds, coverage, observed import/export energy, and observed costs.
A separate `extrapolated_cost` is labelled as an estimate. Missing readings and long reporting gaps
remain unknown. The daily cost sensor is unavailable unless every elapsed closed quarter has at least 98% coverage
and a known price; the card separately shows partial observed
cost and coverage. Older history without coverage metadata is retained as an extrapolated estimate.

When both cumulative import/export meters are selected, their deltas take precedence over sampled
grid power. Counter resets and unavailable meters create missing coverage. Short intervals crossing
a quarter boundary are allocated proportionally; long gaps are not assigned to a tariff by guessing.
Without meters, power samples estimate observed energy and can miss fast changes between samples.

Household power and PV measurements are recorded with coverage and forecast errors. Corrections
always compare measurements with the *uncorrected* forecast, so a correction never cancels itself:

- **Running quarter**: the live average load and PV replace the forecast; the next quarter gets half.
- **Load**: after at least 20 quarters with 95% coverage in the last 12 hours, the next two hours
  receive a correction bounded to ±20%, fading to zero.
- **PV**: the last hour's actual/forecast ratio (bounded 0.3–1.7, ignoring dawn/dusk and curtailed
  quarters) corrects the next three hours, fading to zero. Select all production sensors (for
  example an AC-coupled inverter and the MPPTs) under *PV production power sensors*; without them
  the curtailable inverter's sensor is used.
- **Load profile**: a trimmed mean per hour (highest and lowest 10% of days dropped), so a one-off
  EV session or defrost does not raise that hour for weeks.

Solcast's `forecast_tomorrow` and `forecast_day_3` sensors are added automatically when
`forecast_today` is configured. Energy-only household sensors still support recorder profiles but
do not supply instantaneous load-error feedback. Savings remain forecasts, not measured savings
claims.

### How actions map to the ESS

| Action | ESS writes |
|---|---|
| Self consumption | setpoint = idle setpoint with the original limits, so the battery absorbs forecast errors (extra PV, load spikes); only when the plan deliberately exports part of the surplus or imports part of the load is that direction capped at planned power; always within the live SoC window |
| Hold | max inverter power = 0 (grid covers load); charging blocked only when the plan exports PV on purpose. Used whenever the plan keeps the battery idle while importing: stored energy is worth more later, or the SoC is at the minimum |
| Charge from grid | setpoint = max import, max charge power = planned charge power |
| Discharge to grid | setpoint = −max export, max inverter power = planned discharge power |
| Curtail (negative prices) | max feed‑in = 0, PV limit follows house load (grid import ≈ 50 W) |

Automatic control requires all four ESS numbers: grid setpoint, charge power, inverter power and
feed-in limit. Missing configuration keeps the setup advisory with a Repairs explanation; an offline
essential actuator pauses commands. W and kW are supported (legacy unitless number entities use W).
Unexecutable targets are reported before dispatch instead of silently clamped. Limits are written
before forced setpoints. A SoC report older than five minutes blocks both battery directions;
`last_reported` accepts fresh unchanged readings. An optional ISO `source_timestamp` attribute can
supply the device's report time. The same freshness rule excludes stale measurements from accounting.

At minimum SoC, discharge is blocked; at maximum SoC, charging is blocked. Missing SoC blocks both.
DESS-off acknowledgement is checked each tick and retried at most every 30 seconds. Re-enabling
DESS externally pauses new forced commands and neutralizes previously dispatched commands. Originals
remain persisted until release. Failed restores of replaced actuators have a separate persistent queue,
keyed by original entity ID and units, with exact outstanding values in Repairs and the dashboard.
Readback must confirm recovery before the queue clears, including across options reloads and restarts.

The optimizer conserves energy across small flows, SoC, costs and wear. An unavoidable grid import
violation is shown as `feasible: false`, `grid_violation_kw` per slot, and a Repairs warning. Available
battery assistance is retained; a configured limit is never silently discarded.

> Victron stores these settings in flash; Energy Control only writes on change (a few writes per hour).

## Heat pump and EV (flexible loads)

Configure them under **Heat pump** and **EV charger** in the integration options. Both are planned
together with the battery: they are scheduled on the first plan's prices and energy flows, after
which the battery is re-planned around their shifted consumption.

### Heat pump (SG Ready / EVU)

Select the heat pump's mode entity: a **select** with SG Ready modes (options containing *Normal*,
*EVU*/*Block*, *Comfort* and *Boost* are recognised) or a **switch**/relay on the EVU contact. Disable
other automations that set the same entity, then turn on **Heat pump control**.

- **Block (EVU)** in expensive quarters when catching up afterwards is clearly cheaper: the saving
  is the blocked energy at its price minus the rebound (+5% for the lower COP) in the quarters
  after the block, using the plan's marginal prices (exported PV counts at the export price).
- **Outdoor temperature limits** the block length: full length (default 2 h) from 12 °C, shrinking
  linearly to none at 2 °C, and the daily budget (default 6 h) scales the same way. Unknown
  outdoor temperature means no blocking. Optional indoor sensor: never block below 19 °C inside.
- **Recovery**: after a block, at least as long as the block ran, no new block starts.
- **Comfort** (state 3) when PV is being exported or prices are in the cheapest 15%;
  **Boost** (state 4) only if enabled (not every heat pump supports it) and energy has a negative price.
- Live guards run every 10 s: a block ends immediately when it gets too cold, the house cools
  below the minimum, the block limit is reached or the daily budget is used. Turning control off
  or unloading the integration always returns the heat pump to **Normal**.
- The heat pump's power is refitted every 6 hours (and on `energy_control.replan`) from the last
  *load days* (default 14) of history, so it follows changes in heating curve, setpoints or season
  within one to two weeks. Temperatures colder than any day in that window are extrapolated
  linearly and may be underestimated (heat pump efficiency drops in the cold).
- The heat pump's power is modelled from its power sensor history:
  `power = level(daily outdoor temperature) × shape(hour of day)`. The level is a line fitted on
  *daily* averages (the building's thermal mass smooths hourly temperature swings), and reaches
  zero at the balance temperature above which no heating is needed. The shape is the learned share
  of each hour (night setback, hot water). Future temperatures come from the weather entity's
  hourly forecast, averaged over 24 h. The fit (`model_w_per_degree`, `model_no_heating_above_c`,
  `model_r2`) is shown on the heat pump sensor. Its consumption is subtracted from the household
  load profile so it is not counted twice.

`sensor.energy_control_heat_pump_mode` shows the commanded state with `status` (why), the hours
blocked today, the next planned change and the planned schedule.

### EV charger

A charger **switch** (on = charging allowed), its power and optionally a power sensor, a
car-connected sensor and the car's state-of-charge sensor with battery capacity. Modes
(`select.energy_control_ev_charging`): **manual** (not touched), **smart** (cheapest quarters before
*EV ready by*, exported PV counts at the export price) and **now**. The energy needed comes from the
target SoC, or from *EV energy to charge*, which counts down while charging. If the energy does not
fit before the deadline, charging starts immediately.

## Dashboard

The card is loaded automatically. Add it to any dashboard:

```yaml
type: custom:energy-control-card
hours: 36       # plan hours ahead (12/24/36/48 buttons switch it on the card)
past_hours: 12  # history shown left of "now"
hour_width: 0   # px per hour: 0 = fit the card; e.g. 48 = wider chart that scrolls sideways
tab: timeline   # tab to open on: timeline / usage / money / schedule / history / summary
```

**Timeline** is one chart over past and future: left of the “now” line you see what was planned
for each 15-minute slot next to what actually happened, right of it the plan. **Red = actual**
everywhere. Panes:

- **Corrections** (chips above the chart): the active PV and load correction factors and live values.
- **Price**: bar height is the import price, bar colour is what the battery does then. ▼ marks the
  three cheapest and ▲ the three most expensive bars ahead. Dotted line = spot (market) price,
  thin line = wind forecast (right axis), faded bars = estimated prices. Strips below show planned
  heat pump blocks/comfort and EV charging.
- **Battery**: planned power as solid blocks (▲ charge / ▼ discharge, faded in the past), actual
  power as a red line, and SoC as a blue line on the right axis (dashed = what was planned).
- **Grid**: planned (dashed) and actual (red) power, ▲ import / ▼ export.
- **PV/load**: corrected PV forecast (filled) with the Solcast P10–P90 band, the uncorrected
  forecasts dashed, curtailed PV in red, and measured PV and load left of now.

Hover or tap the chart: the row above it shows that slot's values (plan next to actual for the
past, the reason for a planned decision).

**Usage** stacks base load, heat pump and EV: measured left of now (with the forecast dashed),
predicted right of it. Heat pump blocks are shaded; outdoor temperature (forecast and measured) and
indoor temperature with its minimum are on the right axis, so you can see blocking does not cost
comfort. A table compares predicted (next 24 h) and measured (last 24 h) kWh.

**Money** shows the cumulative cost from midnight: measured, the plan, and without battery, continued
with the plan ahead; a table gives cost, cost without battery and the saving per day.

**Schedule** lists the plan as blocks: back-to-back slots doing the same thing become one row with
average battery power, average import price and SoC at start/end (hover a row for the reason).
**History** starts with where the energy went per day (house from PV / battery / grid, PV to
house / battery / grid, self-sufficiency and self-consumption), then shows past hours newest first, plan next to actual for battery, grid, SoC and cost, with
the cost difference in green (cheaper than planned) or red; narrow cards get a compact layout.
**Summary** starts with battery, planned savings and today's cost, then groups mode, inverter
setpoints, upcoming windows, costs and settings.

The colours split self consumption by what the battery does: **Battery → house** (battery
covers the load), **PV → battery** and **Idle**; **Hold** means the grid covers the house while the
battery is saved. **−/+** zoom out/in: zoomed in, the chart scrolls sideways while the y-axis stays
in place. The card takes the full width in a sections dashboard; for even more room put it in a
*Panel* view or a wide section.

Entities for your own cards: `sensor.energy_control_action` (with `reason`), `…_planned_grid_power`,
`…_planned_battery_power`, `…_target_soc`, `…_next_grid_charge`, `…_next_grid_discharge`,
`…_plan_cost`, `…_plan_savings`, `…_energy_cost_today`, `…_pv_forecast_accuracy` and
`…_load_forecast_accuracy` (7 days, energy weighted, attributes `bias` = actual/forecast,
`quarters`, `actual_kwh`, `forecast_kwh`), `sensor.energy_control_plan`
(attribute `forecast`: compact slot list, not recorded), `binary_sensor.energy_control_control_active`,
`binary_sensor.energy_control_curtailing_pv`.

<details><summary>apexcharts-card example</summary>

```yaml
type: custom:apexcharts-card
graph_span: 36h
span: { start: minute }
yaxis: [{ id: soc, min: 0, max: 100 }, { id: kw, opposite: true }]
series:
  - entity: sensor.energy_control_plan
    name: Planned SoC
    yaxis_id: soc
    data_generator: return entity.attributes.forecast.map(s => [new Date(s.start).getTime(), s.soc]);
  - entity: sensor.energy_control_plan
    name: Battery (W)
    type: column
    yaxis_id: kw
    data_generator: return entity.attributes.forecast.map(s => [new Date(s.start).getTime(), s.battery_w]);
```
</details>

## Services

- `energy_control.replan` — recompute now (also refreshes the load profile).
- `energy_control.override` — run an action (`charge_grid`, `discharge_grid`, `hold`, `self_consume`) for a duration, optional power.
- `energy_control.clear_override`.
- `energy_control.cheapest_window` — returns the cheapest time to run an appliance: `duration`,
  `power` (W), optional `earliest` and `latest_end`, `contiguous: false` to allow spread-out
  quarters (EV). Uses the plan, so PV that would otherwise be exported counts at the export price.
  Response: `start`, `end`, `average_price`, `cost`, `energy_kwh`, `price_estimated`, `runs`.

```yaml
# Example: start the dishwasher in the cheapest 2 h before 07:00
- action: energy_control.cheapest_window
  data:
    duration: "02:00:00"
    power: 800
    latest_end: "{{ today_at('07:00') + timedelta(days=1) }}"
  response_variable: window
- wait_template: "{{ now() >= as_datetime(window.start) }}"
- action: switch.turn_on
  target:
    entity_id: switch.dishwasher
```

## Development

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv -r requirements_test.txt
.venv/bin/pytest -q
```

CI runs the full integration suite with the pinned fixture package on the tested minimum
(2026.10.4). A second job checks every integration module and the pure optimizer, control and parser
tests on current Home Assistant (2026.11.2). The published fixture package currently pins 2026.10.4
and cannot load against 2026.11.2; full current-version fixture tests should be enabled when a matching
package is published. `scripts/check_compatibility.py` verifies module imports independently.

The optimizer, action mapping and attribute parsers are pure Python. Native Nord Pool and live
telemetry adapters use Home Assistant APIs and have mocked integration tests.
