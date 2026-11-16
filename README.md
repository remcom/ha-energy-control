# Energy Control

A local, cost-optimizing battery scheduler for Home Assistant — a self-hosted alternative to
Victron VRM Dynamic ESS. Every 15 minutes it plans the next 24–48 h from dynamic prices, solar
forecast and your own consumption history, executes the plan on a Victron ESS and can curtail an
AC-coupled PV inverter when export prices go negative.

## Features

- **Optimizer** — dynamic programming over battery state of charge per 15‑minute slot. Accounts
  for charge/discharge efficiency, battery wear (€/kWh) and an arbitrage margin, so grid charging
  and battery→grid export only happen when the spread actually pays.
- **Strategies (switches)** — grid charging · battery to grid (arbitrage) · curtail PV at negative prices.
- **Modes** — `off` · `advisory` (plan only, no writes) · `auto` · `self_consumption` · `force_charge` · `force_discharge` · `hold`.
- **Dynamic prices** — Tibber Prices (15‑min), Nord Pool, ENTSO‑e, EnergyZero and other sensors that
  expose a price list in their attributes. All‑in or spot prices (+ markup, energy tax, VAT).
- **Export compensation** — netting (NL *saldering*, export = import price) with an end date, after
  which it switches to `spot × factor − fee`; or a fixed price.
- **Solar forecast** — Solcast (median/P10/P90), Forecast.Solar, Open‑Meteo Solar Forecast.
- **Load forecast** — hourly profile (weekday/weekend) from recorder statistics of your consumption sensor.
- **Victron control** — via the core Victron GX integration entities (auto-suggested): grid
  setpoint, max charge power, max inverter power, max feed‑in. VRM Dynamic ESS is switched off
  while Energy Control is in charge and restored afterwards.
- **PV curtailment** — feedback limiter on any `number` power-limit entity (W or %), e.g. a Solis
  inverter seen by the GX as a PV inverter.
- **Safe by default** — starts in `advisory`. Original settings are saved before taking over and
  restored when switching to `off`/`advisory`, unloading, or stopping Home Assistant. Stale prices
  or SoC fall back to plain self consumption and raise a repair issue on write failures.
- **Dashboard card** — bundled (no extra install): one timeline with plan vs actual, summary.

## Install

1. HACS → ⋮ → *Custom repositories* → add this repository as *Integration*.
2. Install **Energy Control**, restart Home Assistant.
3. *Settings → Devices & services → Add integration → Energy Control*.

## Configuration

| Step | What |
|---|---|
| Prices | Price sensor with forecast attributes, taxes, export model (netting end date, spot factor/fee, fixed). |
| Solar | One or more forecast sensors (Solcast: select *today*, *tomorrow*, *day 3* sensors). Forecasts of all selected sensors are **added up** (e.g. one per PV array), so don't select two sensors that cover the same array and period. |
| Battery | SoC sensor, usable capacity, max charge/discharge power, efficiencies, battery power sensor. |
| Consumption & grid | Household consumption sensor (without battery), grid power sensor (+ = import), grid limits. |
| Victron ESS control | Grid setpoint, charge/inverter/feed-in limits, DESS mode select, PV limiter. Leave the setpoint empty for advisory-only use. |

Everything can be changed later under *Configure*. Runtime tuning is done with entities:
minimum/maximum SoC, battery wear cost (ct/kWh discharged) and arbitrage margin (ct/kWh).

Reported costs (plan cost, planned vs actual cost) are what you pay for grid energy. Battery
wear is reported separately (`wear_cost` in the plan summary) and subtracted from the savings.
The arbitrage margin is only a hurdle for the optimizer: it is never part of a reported cost.

### How actions map to the ESS

| Action | ESS writes |
|---|---|
| Self consumption | setpoint = idle setpoint, limits = original |
| Hold | max inverter power = 0 (grid covers load); charging blocked only when the plan exports PV on purpose. Used whenever the plan keeps the battery idle while importing: stored energy is worth more later, or the SoC is at the minimum |
| Charge from grid | setpoint = max import, max charge power = planned charge power |
| Discharge to grid | setpoint = −max export, max inverter power = planned discharge power |
| Curtail (negative prices) | max feed‑in = 0, PV limit follows house load (grid import ≈ 50 W) |

> Victron stores these settings in flash; Energy Control only writes on change (a few writes per hour).

## Dashboard

The card is loaded automatically. Add it to any dashboard:

```yaml
type: custom:energy-control-card
hours: 36       # plan hours ahead (12/24/36/48 buttons switch it on the card)
past_hours: 12  # history shown left of "now"
hour_width: 0   # px per hour: 0 = fit the card; e.g. 48 = wider chart that scrolls sideways
```

**Timeline** is one chart over past and future: left of the “now” line you see what was planned
for each 15-minute slot next to what actually happened, right of it the plan. Everywhere **bars /
blue = plan** and **red = actual**. Panes: action, battery power (▲ charge / ▼ discharge), grid
power (▲ import / ▼ export), SoC, price, PV & load forecast. Hover a slot for details: the reason
for a planned decision, or a plan-vs-actual table for a past slot. **Summary** lists mode, next
windows, cost & savings and the current ESS setpoints.

The action band splits self consumption by what the battery does: **Battery → house** (battery
covers the load), **PV → battery** and **Idle**; **Hold** means the grid covers the house while the
battery is saved. **−/+** zoom out/in: zoomed in, the chart scrolls sideways while the y-axis stays
in place. The card takes the full width in a sections dashboard; for even more room put it in a
*Panel* view or a wide section.

Entities for your own cards: `sensor.energy_control_action` (with `reason`), `…_planned_grid_power`,
`…_planned_battery_power`, `…_target_soc`, `…_next_grid_charge`, `…_next_grid_discharge`,
`…_plan_cost`, `…_plan_savings`, `…_energy_cost_today`, `sensor.energy_control_plan`
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

## Development

```bash
uv venv --python 3.14 .venv && uv pip install --python .venv -r requirements_test.txt
.venv/bin/pytest -q
```

The optimizer (`optimizer/`) and providers (`providers/`) are pure Python and unit-tested
without Home Assistant.
