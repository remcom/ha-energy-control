# Follow-up to the 16 November 2026 reliability report

All eight findings and the three follow-on opportunities are implemented. No live device
configuration was changed during implementation.

| Report item | Result | Regression coverage |
|---|---|---|
| 1. Live SoC protection | Common guard for automatic, manual and override actions; block the prohibited actuator direction and request a bounded replan | `test_reliability.py`: automatic limits/recovery, unavailable/nonfinite/out-of-range SoC, overrides, fresh unchanged/stale reports |
| 2. DESS handoff | Persist originals before takeover; require off readback; retry at most every 30 seconds; detect external re-enablement; neutralize earlier forced commands | DESS failure, delayed acknowledgement, unsupported options, existing restart tests |
| 3. Executable capabilities | Require setpoint and charge/inverter/feed-in limits for all control actions, including SoC guards; validate availability, W/kW and target ranges before dispatch | Incomplete setups, lost essential actuators, wrong units, out-of-range targets, sub-100 W mapping |
| 4. Export tariff ambiguity | Never reuse all-in import as raw spot; local-date netting transition; warn 60 days ahead; suppress unknown-price battery export and incomplete monetary estimates | Local midnight, negative raw spot, fixed/spot contracts, missing spot, override suppression and repair recovery |
| 5. Low-load trajectory | Include exact sub-step flows in backward planning and propagate continuous energy through reconstruction | Joined SoC endpoints, DC energy, AC balance, cost, wear, power/SoC bounds |
| 6. Grid infeasibility | Retain assistance; explicitly flag unavoidable import violations and curtail PV for export limits; preserve strategy | Feasible assistance, unavoidable overload, export curtailment |
| 7. Compatibility | Tested minimum HA 2026.10.4 / Python 3.14; minimum/current CI matrix | Full suite on minimum; all module imports and pure tests on 2026.11.2 |
| 8. Input quality | Finite numeric conversion, valid durations and SoC range, recognized explicit price units/currencies, report-age checks | NaN/infinity, invalid intervals/currencies, SoC and timestamp cases |
| Native Nord Pool | Core response action for today/tomorrow, explicit area/EUR, MWh conversion, cache and publication-aware retry | Hourly/quarterly intervals, missing tomorrow, currency request, midnight cache rollover, native-only config/coordinator |
| Actual cost and forecast quality | Persist coverage and observed energy/cost; separate extrapolation; prefer optional cumulative meters; expose measured load/PV errors; bounded correction after sufficient observations | Partial/missing/full coverage, persistence, meter reset and precedence, correction threshold/bounds |
| Replaced-actuator recovery | Separate persistent queue by original entity ID and units; exact Repairs details; retry and clear only after readback | Failed replacement restore across options reload and simulated restart |

Validation on Python 3.14.10:

- Home Assistant 2026.10.4: **152 tests passed** using fixture package 0.13.375.
- Home Assistant 2026.11.2: **69 pure tests passed**, all 30 integration modules imported.
- Ruff check and format check passed; bundled JavaScript passed `node --check`.

The published fixture package pins HA 2026.10.4 and cannot load its internal fixtures on
2026.11.2. Current-version CI therefore checks imports and pure behavior; full fixture coverage
on current HA is still a follow-up when a compatible package becomes available.

Implementation choices: retain advisory planning for incomplete hardware, use five minutes as
the live telemetry age limit, use a 60-day tariff warning window, and bound recent-load correction
to ±20% over the next two hours after 20 quarters with at least 95% load coverage. Legacy unitless
prices retain EUR/kWh and unitless power actuators retain W. Explicit foreign currencies require
conversion. These choices are documented in the README.

Before enabling unattended control on an installation, verify real actuator readback, DESS
handoff, telemetry reporting cadence, meter units and the actual export contract. Live Home
Assistant verification was unavailable in this run. No measured monetary saving is claimed.
