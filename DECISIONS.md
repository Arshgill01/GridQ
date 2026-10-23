# DECISIONS.md

Each entry: what changed vs the spec, why, and the measured before/after. Every tuning
choice is made on the TUNE days (1–10) and only then reported on the REPORT days (11–30).
Numbers come from `python -m gridq.bench.run_days` (results in `data/bench/`).

## D1. Headline metric: total cost of service, not the energy bill

**Spec:** `cost_rs = grid·tariff + diesel`; `captured_pct` on `cost_rs`.

**Problem:** that metric rewards *not serving load*. A controller that sheds the hostels, or
lets the EV miss its deadline, buys less energy and looks cheaper. It also ignores battery
wear, so a rule that cycles the battery hard gets free storage.

**Change:** `total_cost_rs = energy bill + diesel + peak-demand charge + battery wear +
VoLL × unserved inflexible energy + slack price × missed flexible energy (+ end-of-run
battery settlement)`. The Proof screen leads with this and still shows the bill, wear and
unserved kWh as separate columns. `captured_pct` is reported on both, and the total is the headline.

**One-sentence version for a judge:** "we charge every controller for the energy it buys *and* for the
energy it fails to deliver, at the same prices the optimiser uses."

## D2. Direct sparse LP on HiGHS instead of CVXPY

The LP is assembled as scipy sparse matrices and solved by HiGHS (`scipy.optimize.linprog`).
It's the same model as spec §8, with duals from `res.eqlin.marginals`. 96-step solve: ~7 ms on an
M1 laptop (CVXPY's canonicalisation alone is typically 50–200 ms for this size). That's one fewer
heavy dependency on the ARM board, and the 28,800 benchmark solves run in minutes.

## D3. Outage-aware fan-tree MPC (the main controller improvement)

*(numbers filled in after the tune-day comparison)*

The deterministic MPC plans as if the grid stays up, so an evening peak-shave drains the
battery exactly when an outage would hurt most. The spec's fix is a fixed P90 clinic reserve
(~20 kWh, 40 % of the battery), which is blunt.

The fan tree is a trunk (grid stays up) plus branches where the grid fails at +15 min, 30 min, 1 h, 2 h
and 4 h, each lasting 45 min or 2 h. Each branch shares every decision with the trunk until
its failure step (non-anticipativity), then re-dispatches on its own. Branch weights are the
probability that the *first* outage starts in that window, computed from the outage
statistics (Poisson rate, start-hour range), never from the realised schedule. The objective is the
expected cost over the tree.

**Judge sentence:** "every 15 minutes we plan against the grid failing at several times in the next
few hours, weighted by how often it really fails, so the battery keeps exactly as much as an
outage is worth."

## D4. Benchmark protocol: continuous runs, battery settled once

Independent 24 h days reward draining the battery before midnight, so the protocol uses continuous runs instead.
Each outage seed is one continuous 30-day run, the same for every controller. Battery energy
carries over and is settled once at the end (`bench.settle_rs_per_kwh`, same for all).
Consequence: "Oracle ≤ GridQ" holds in aggregate but is not guaranteed on every single day
(path dependence, and the LP objective is not identical to the metric). Per-day exceptions are
counted and reported, not hidden.

## D5. Baselines made realistic (stronger, not weaker)

- **B0:** a backup-mode inverter recharges from the grid when below full (within the import
  limit). Without this, one morning outage left B0's battery empty for the rest of the run,
  which is a strawman.
- **B1:** also refills from the grid in the cheap solar-tariff hours, and discharges to keep
  import under the 40 kW limit (a careful facility manager would program both).

## D6. Forecaster: direct multi-horizon quantile GBM with conformal P90

*(numbers filled in after training)*

The model predicts all 95 horizons in one batched call per feeder, with horizon as a feature,
instead of 96 recursive calls. Features are causal only (target-time calendar including the published
IIIT-D working-day calendar, same time yesterday and last week, level before the origin).
P90 is split-conformally calibrated on the last 45 training days so its coverage is ≈ 90 % by
construction, then reported on held-out days.

## D7. Site coordinates

The spec's lat/lon was Chandigarh, but I-BLEND is IIIT-Delhi. Solar is fetched for IIIT-Delhi
(28.5459 N, 77.2732 E) so demand and weather come from the same place *and* the same days.

## D8. Diesel tank is refilled daily (plant-model fix)

First real-data run: GridQ came out *worse* than B1, with 2,214 kWh unserved on the tune days. The diagnosis
(`grid up` shed steps = 0) showed every shed step was in an outage, after the 100 L tank had run
dry. At 1.5 outages/day the tank lasts about 5 days and is never refilled, so from day 6 onward
every controller sheds in every outage, and whoever burnt fuel early (GridQ, which correctly
prefers ₹27/kWh diesel to ₹40/kWh shedding) pays later. A real campus tops up its genset tank.
Now: `diesel.refill_daily: true` (tank back to 100 L at 00:00, every litre still costed at ₹90),
and the LP has one fuel budget per calendar day in its horizon (trunk and branches).

## D9. Single-threaded OpenMP

sklearn's GBM predict fans out to OpenMP threads per tree; with 5 benchmark processes (or the web
server on the 4-core board) this made one 95-row forecast take 700 ms. With `OMP_NUM_THREADS=1`
(set in `gridq/__init__.py`) it takes 2.3 ms per model (14 ms for all six). The LP solve is about 10 ms.

## Observation: grid arbitrage never pays here (and the LP knows it)

Buying at the 6.8 ₹/kWh solar-hour tariff to discharge at 9.6 ₹ peak costs
6.8/(0.95·0.95) + 2.3·(1/0.9025 + 1) ≈ 12.4 ₹ per delivered kWh, which is more than 9.6 ₹. So
GridQ deliberately does *not* cycle the battery for ToD arbitrage (B1 does, and pays for it in wear).
The battery's value is PV shifting, the 40 kW cap, the demand charge and, above all, outages.
The dashboard explains this with a re-solve: "Discharging 10 kW now would cost ₹X more".
