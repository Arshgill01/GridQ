# GRIDQ_SPEC.md — Build specification for Claude Code

> **How to use this file.** Put it at the repo root (or rename it `CLAUDE.md`). Build in the phase order in §17. Do not change the interfaces in §10 without asking the team. Every number shown to judges must be computed by this code, never hard-coded. Everything must run **offline**.

---

## 0. One-paragraph summary

GridQ is an offline edge energy controller for a small campus microgrid. It runs on an **Arduino UNO Q**. The Linux side (Qualcomm QRB2210) runs a **digital twin** of the microgrid, a **forecaster**, a **rolling-horizon LP optimizer (MPC)**, an **explainer** and a **web dashboard**. The STM32U585 MCU side is a **safety supervisor**: it drives the physical outputs, trips the grid relay locally, and falls back to a safe load mask if the Linux "brain" stops sending heartbeats. An **ESP32** is a separate "health-centre unit" over Wi-Fi, with an LCD, a servo battery gauge and an ultrasonic EV-bay sensor. GridQ is benchmarked against two realistic baselines and a perfect-foresight upper bound on identical simulated days.

**Real data:**
- Hostel and academic demand comes from the **I-BLEND** dataset, measured on an Indian campus (IIIT-Delhi).
- Solar comes from **NASA POWER**, for the same calendar dates.
- Tariffs follow India's Time-of-Day rules.

**Forecasting:** the demand forecaster is a small **quantile gradient-boosting** model (P50 and P90) trained on I-BLEND. Its P90 output sizes the clinic's battery reserve. There are no CNNs or deep networks (§18).

**Evaluation design:** rule-based baseline vs MPC vs a perfect-foresight bound. This follows Panda et al. 2024 (§20).

**Honesty rule:** the electrical plant (solar, battery, diesel generator, feeders) is **simulated**. The controller, the optimizer, the MCU supervisor, and the physical inputs and outputs are **real**. The dashboard must permanently show the badge `SIMULATED PLANT · REAL CONTROLLER`. Never present simulated values as sensor readings.

---

## 1. Hardware inventory and roles

| Part | Qty | Role |
|---|---|---|
| Arduino UNO Q (QRB2210 Linux + STM32U585 MCU) | 1 | Brain, twin, web server (Linux); safety supervisor, matrix, LEDs, relay, buzzer (MCU) |
| 1-channel 5 V relay module (opto, usually active-LOW) | 1 | **Grid tie / transfer switch.** Energised = campus islanded (grid disconnected) |
| Jumper wire, the "grid cable" | 1 | Judge pulls it out = physical grid outage |
| Buzzer (active or passive; verify) | 1 | Audible alarms |
| ESP32 DevKit (ESP32-WROOM) | 1 | Health-centre node: LCD, servo, ultrasonic, I2C sensor; talks to the UNO Q over Wi-Fi |
| 16×2 LCD with PCF8574 I2C backpack | 1 | Clinic status screen |
| SG90 micro servo | 1 | Needle gauge for battery SoC on a cardboard dial |
| HC-SR04 ultrasonic sensor | 1 | EV-bay occupancy sensor, which triggers the "EV fleet arrives" event |
| Small blue I2C sensor module (identify by I2C address) | 1 | Optional live input: BH1750 = light, BMP/BME280 = temperature, MPU6050 = unused |
| Resistors 1 kΩ, 2 kΩ (or 2× 1 kΩ) | 1 set | HC-SR04 ECHO voltage divider |
| Arduino UNO R3 clone | 1 | **Spare only.** Its 5 V pin is a backup supply for the servo. Not a controller |
| Breadboard, jumpers, cardboard panel | — | Mounting and wiring |
| Phone/laptop | 1+ | Dashboard client; phone or laptop hotspot (**2.4 GHz**) |

**Safety:** everything is 5 V DC. Nothing from mains or the power strip is ever connected to the relay contacts.

---

## 2. Wiring

### 2.1 UNO Q (MCU header)

| UNO Q pin | Connects to | Notes |
|---|---|---|
| `D2` | Relay `IN` | Output. Active-LOW by default (`RELAY_ACTIVE_LOW = true` in config) |
| `5V` | Relay `VCC` | One coil is about 70 mA, so this is OK from the UNO Q |
| `GND` | Relay `GND` | |
| `D3` | Grid-cable jumper, other end → `GND` | `INPUT_PULLUP`. LOW = cable present = grid available. HIGH = pulled = outage |
| `D4` | Buzzer `+` | Buzzer `−` → `GND` |
| built-in | 8×13 LED matrix | Mimic panel (§9.2) |
| built-in | LED3, LED4 (RGB, MCU-controlled, active-LOW) | Status (§9.3). Use pin names from the App Lab Blink example |
| built-in | LED1, LED2 (Linux-controlled via `/sys/class/leds`) | Optional; fallback status if Bridge fails |

**Relay contacts:** the grid tie is wired through **COM–NC**, so a de-energised relay means "connected to grid" (fail-safe: if the controller loses power, the site stays on grid). The contacts carry no load in the demo; the click and the module LED are the indicator. Optionally, 5 V → 330 Ω → LED → COM/NC gives a "grid connected" lamp.

**Fallback if the relay won't trigger from 3.3 V:** D2 → 1 kΩ → BC547/2N2222 base, emitter → GND, collector → relay `IN`. This inverts the logic: set `RELAY_ACTIVE_LOW = false`.

### 2.2 ESP32

| ESP32 pin | Connects to | Notes |
|---|---|---|
| `GPIO21` (SDA) | LCD backpack `SDA` + blue sensor `SDA` | Shared I2C bus |
| `GPIO22` (SCL) | LCD backpack `SCL` + blue sensor `SCL` | |
| `VIN` (5 V from USB) | LCD `VCC`, HC-SR04 `VCC`, servo red | |
| `3V3` | Blue sensor `VCC`/`VIN` | |
| `GND` | All grounds on the ESP32 side | |
| `GPIO18` | Servo signal (orange) | ESP32Servo library, 50 Hz, 500–2400 µs |
| `GPIO5` | HC-SR04 `TRIG` | |
| `GPIO19` | HC-SR04 `ECHO` **via divider** | ECHO → 1 kΩ → GPIO19 → 2 kΩ → GND (never connect ECHO directly) |
| `GPIO2` | Onboard LED | Link status |

If the ESP32 resets when the servo moves, power the servo red wire from the **UNO R3 clone's 5 V** and connect the UNO R3 GND to the ESP32 GND.

### 2.3 Power and grounding

- UNO Q: USB-C from a 5 V 3 A charger.
- ESP32: USB from the laptop or a power bank.
- The UNO Q and ESP32 share **no signal wires** (Wi-Fi only), so they do **not** need a common ground. Within each board's subsystem, all grounds are common.

### 2.4 I2C address map (run the scanner first)

| Address | Device | Config value `sensor.type` |
|---|---|---|
| `0x27` / `0x3F` | LCD backpack | — |
| `0x23` / `0x5C` | BH1750 light | `bh1750` |
| `0x76` / `0x77` | BMP280 / BME280 | `bmp280` / `bme280` |
| `0x68` | MPU6050 | `none` (don't use) |

---

## 3. System architecture

```
 Phone / laptop browser ──Wi-Fi──┐
                                 │  HTTP + SSE  (port 8000)
 ESP32 clinic node ──Wi-Fi───────┤
   LCD · servo · HC-SR04 · I2C   │
                                 ▼
 ┌──────────────── UNO Q · Linux (QRB2210) ─────────────────┐
 │  gridq-io  (always running)                               │
 │    FastAPI web + SSE · Plant twin · Event log · Benchmark │
 │    Replay · Bridge client (sole owner of Bridge)          │
 │    Supervises gridq-brain as a child process              │
 │                        ▲ HTTP localhost                   │
 │  gridq-brain (killable)│                                  │
 │    Forecaster · MPC LP · Explainer · Heartbeat (500 ms)   │
 └───────────────────────┬──────────────────────────────────┘
                         │ Bridge RPC (arduino-router)
 ┌───────────────────────▼──────────────────────────────────┐
 │  STM32U585 MCU sketch: safety supervisor                 │
 │   heartbeat watchdog (3 s) · local grid trip · relay D2  │
 │   grid cable D3 · buzzer D4 · LED matrix · LED3/LED4     │
 └──────────────────────────────────────────────────────────┘
```

**Key principle:** the MCU is the **authority for outputs** (relay, applied load mask). The twin reads the *applied* mask and relay state back from the MCU every step, the way a real plant would. The brain only proposes.

---

## 4. Repository layout

```
gridq/
├── GRIDQ_SPEC.md
├── config/
│   ├── params.yaml          # plant + economics (FROZEN before tuning)
│   └── hardware.yaml        # pins, polarity, servo range, LCD addr, sensor type
├── gridq/
│   ├── plant/
│   │   ├── twin.py          # Twin class: step(control, applied_mask) -> StepResult
│   │   ├── profiles.py      # demand profiles, solar loading, seeded noise
│   │   ├── events.py        # event types + application to twin
│   │   └── outages.py       # seeded outage schedule generator
│   ├── control/
│   │   ├── base.py          # Controller interface
│   │   ├── baseline_b0.py   # inverter default
│   │   ├── baseline_b1.py   # smart ToD rules
│   │   ├── safe.py          # SAFE-mode rule controller (used when MCU is in SAFE)
│   │   ├── lp.py            # LP builder (shared by MPC + Oracle)
│   │   ├── mpc.py           # GridQ controller (forecasts)
│   │   ├── oracle.py        # perfect-foresight controller (true future)
│   │   └── explain.py       # counterfactual re-solves, shadow price, survival time
│   ├── forecast/
│   │   ├── features.py      # calendar + lag features (shared by train and inference)
│   │   ├── train.py         # CLI: train P50/P90 quantile models on I-BLEND train period
│   │   ├── demand.py        # loads models, predicts P50/P90; seasonal-naive fallback
│   │   └── solar.py
│   ├── bench/
│   │   ├── run_days.py      # CLI: run all controllers over N days -> results.json
│   │   ├── replay.py        # replay a live session log through B0 and GridQ
│   │   └── metrics.py
│   ├── io/
│   │   ├── app.py           # FastAPI app, SSE, sim clock thread, brain supervisor
│   │   ├── bridge.py        # Bridge wrapper (+ MockBridge for laptop dev)
│   │   └── node_format.py   # LCD line formatting for ESP32
│   ├── brain/
│   │   └── main.py          # separate process: poll state, solve, post plan, heartbeat
│   └── common/
│       ├── schema.py        # dataclasses / pydantic models for State, Plan, Event
│       └── config.py
├── sketch/
│   └── sketch.ino           # UNO Q MCU safety supervisor
├── esp32/
│   └── clinic_node/
│       ├── clinic_node.ino
│       └── secrets.h.example  # WIFI_SSID, WIFI_PASS, GRIDQ_HOST (not committed)
├── web/
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── vendor/uplot.min.js, uplot.min.css   # vendored, no CDN
├── data/
│   ├── iblend/raw/          # downloaded I-BLEND files (not committed if large)
│   ├── demand_15min.csv     # processed: timestamp, hostel_a, hostel_b, academic (kW, scaled)
│   ├── solar_hourly.csv     # NASA POWER export for the same dates (see §6.2)
│   ├── models/demand_p50.joblib, demand_p90.joblib
│   └── bench/results.json   # generated
├── tools/
│   ├── fetch_nasa_power.py  # run once with internet
│   ├── prepare_iblend.py    # raw I-BLEND -> data/demand_15min.csv (resample, map, scale, split)
│   └── i2c_scan.ino
└── tests/
    ├── test_twin.py
    ├── test_lp.py
    ├── test_determinism.py
    └── test_metrics.py
```

On the UNO Q, package it as an **App Lab app** (python entry + `sketch/`), because Bridge is available there. Keep the code runnable on a laptop with `MockBridge` for development (`GRIDQ_MOCK_BRIDGE=1`).

---

## 5. Configuration files

### 5.1 `config/params.yaml` (freeze before any controller tuning)

```yaml
time:
  step_minutes: 15
  steps_per_day: 96
  demo_start_hour: 16          # live demo starts at 16:00 to reach the evening peak fast

site:
  name: "Campus Block A"       # rename to a real block on your campus
  lat: 30.77                   # Chandigarh University approx; change if needed
  lon: 76.57

grid:
  import_limit_kw: 40
  tariff_rs_per_kwh:           # India ToD structure (solar hours cheaper, peak dearer)
    solar:  {from: 9,  to: 17, rate: 6.8}
    peak:   {from: 18, to: 23, rate: 9.6}
    normal: {rate: 8.0}
  peak_charge_rs_per_kw: 15    # per horizon/day, applied to max import
  emission_kg_per_kwh: 0.71

solar:
  kwp: 30
  performance_ratio: 0.78

battery:
  capacity_kwh: 50
  soc_min_frac: 0.10
  soc_max_frac: 0.95
  soc_init_frac: 0.60
  p_charge_max_kw: 25
  p_discharge_max_kw: 25
  eta_charge: 0.95
  eta_discharge: 0.95
  wear_rs_per_kwh: 2.3         # applied to charge + discharge throughput

diesel:
  p_max_kw: 24
  litres_per_kwh: 0.30
  rs_per_litre: 90             # => 27 Rs/kWh
  tank_litres: 100
  co2_kg_per_litre: 2.68

loads:                         # priority 1 = most critical
  clinic:    {priority: 1, kind: inflexible, voll: 500}
  pump:      {priority: 2, kind: flexible, p_kw: 7.5, energy_kwh: 22.5, window: [5, 20], b0_start: 6, slack_rs: 150}
  hostel_a:  {priority: 3, kind: inflexible, voll: 40, fair: true}
  hostel_b:  {priority: 3, kind: inflexible, voll: 40, fair: true}
  academic:  {priority: 3, kind: inflexible, voll: 40, fair: true}
  ev:        {priority: 4, kind: session, p_kw: 14.8, energy_kwh: 30, arrive: 20, deadline: 8, slack_rs: 60}
  heater:    {priority: 5, kind: flexible, p_kw: 6, energy_kwh: 12, window: [22, 7], b0_start: 4, slack_rs: 30}

shed_order_baseline: [heater, ev, academic, hostel_b, hostel_a, pump, clinic]  # fixed, no fairness

data:
  iblend_dir: data/iblend/raw
  demand_csv: data/demand_15min.csv
  building_map:                # GridQ feeder -> I-BLEND building (verify names after download)
    hostel_a: boys_hostel
    hostel_b: girls_hostel
    academic: academic_block
  scale_to_p95_kw: {hostel_a: 20, hostel_b: 18, academic: 25}   # keep real shape, scale to our block size
  min_day_coverage: 0.95       # drop days with >5% missing samples
  test_days: 30                # last 30 good days of the latest year = held-out benchmark days
  # everything before the test days = training data for the forecaster

outages:
  per_day_lambda: 1.5          # Poisson
  start_hour_range: [6, 23]
  duration_min_range: [20, 180]  # rounded to 15 min

mpc:
  horizon_steps: 96
  terminal_value_rs_per_kwh: 6.0
  reserve_hours_clinic: 4
  reserve_penalty_rs_per_kwh: 50
  reserve_quantile: p90        # clinic reserve sized on P90 demand forecast
  curtail_penalty_rs_per_kwh: 1.0
  outage_assumed_remaining_steps: 4
  solve_timeout_s: 3.0
  fairness_burden_per_hour: 1.0   # burden = 1 + shed_minutes/60

bench:
  days: 30
  tune_days: [1, 10]
  report_days: [11, 30]
  seed_base: 1000
  outage_seeds: 5              # each real day x 5 outage patterns = 150 scenario-days (100 on report days)

safety:
  heartbeat_timeout_ms: 3000
  grid_reclose_delay_ms: 5000
  brain_restart_delay_s: 12
  safe_mask_loads: [clinic, pump]
```

### 5.2 `config/hardware.yaml`

```yaml
unoq:
  relay_pin: 2
  relay_active_low: true
  grid_cable_pin: 3
  buzzer_pin: 4
  buzzer_type: active          # or passive (use tone())
esp32:
  sda: 21
  scl: 22
  servo_pin: 18
  servo_min_deg: 10
  servo_max_deg: 170
  trig_pin: 5
  echo_pin: 19
  ev_threshold_cm: 12
  ev_consecutive: 3
  ev_cooldown_s: 10
  lcd_addr: 0x27
  sensor:
    type: none                 # none | bh1750 | bmp280 | bme280
    addr: 0x23
web:
  port: 8000
```

The ESP32 reads its values from compile-time constants in `clinic_node.ino` that mirror this file.

---

## 6. Plant twin

### 6.1 Time
- Continuous timeline of 15-minute steps; `dt = 0.25 h`. A "day" for metrics is 00:00–24:00 local.
- Benchmark mode runs `bench.days` consecutive days, headless and as fast as possible.
- Live mode advances 1 step per real second at 1× (speeds 0.5/1/2/4×, pause, reset). It starts at `demo_start_hour`.

### 6.2 Solar
- `tools/fetch_nasa_power.py` downloads hourly `ALLSKY_SFC_SW_DWN` (GHI, W/m²) and `CLRSKY_SFC_SW_DWN` (clear-sky GHI) for `site.lat/lon`. Cover the **same calendar dates** as the I-BLEND days used for training and benchmarking, so weather and demand come from the same real days. Use local solar time, from the NASA POWER hourly point API (`https://power.larc.nasa.gov/api/temporal/hourly/point?parameters=ALLSKY_SFC_SW_DWN,CLRSKY_SFC_SW_DWN&community=RE&latitude=..&longitude=..&start=YYYYMMDD&end=YYYYMMDD&format=CSV&time-standard=LST`). It saves `data/solar_hourly.csv` with columns `timestamp,ghi,clr`.
- `pv_kw = kwp × ghi/1000 × performance_ratio`, linearly interpolated to 15 minutes. Same for clear-sky (`pv_clear_kw`).
- **Fallback** if the CSV is missing: a synthetic clear-sky bell (`sin` between 06:00 and 18:00, peak 1000 W/m²) × a seeded daily clearness factor U(0.5, 1.0). Log a warning.

### 6.3 Demand profiles (kW)

**Hostels and academic block: real I-BLEND data.**

I-BLEND is a published electricity dataset from the IIIT-Delhi campus. It has per-building power for academic, hostel and other buildings, at roughly minute resolution, over several years (Rashid, Singh & Singh, *Scientific Data* 2019; files on figshare, §20).

`tools/prepare_iblend.py` runs once on a laptop with internet:
1. **Inspect the downloaded files first.** Print building names, units, timezone, resolution and date range. Then set `data.building_map` accordingly. Don't assume a column layout.
2. Convert to kW. Resample to 15-minute means in local time (IST).
3. Drop days below `min_day_coverage`. Fill short gaps (≤ 30 min) by interpolation, never longer ones.
4. Scale each series so its 95th percentile equals `scale_to_p95_kw`. This keeps the real shape and day-to-day variation, sized to our block.
5. Split chronologically. The last `test_days` good days are held out for the benchmark; everything earlier is forecaster training data. **No benchmark day may appear in training.**
6. Write `data/demand_15min.csv` and a `data/demand_meta.json` (source, buildings, date ranges, scale factors) for the dashboard's provenance line.

**Clinic** (no public data, so it is modelled, and the dashboard says so): 3.5 kW, plus 1.0 kW between 08:00 and 20:00, plus ±0.3 kW fridge cycling (square wave, 30-minute period), × seeded noise `N(1, 0.03)`.

**Fallback** if I-BLEND is unavailable: synthetic shapes, with a visible warning in the dashboard and in results.json.
- `hostel_a`: 8 kW base; +6 kW between 06:00 and 09:00; +12 kW between 18:00 and 24:00.
- `hostel_b`: the `hostel_a` shape shifted +30 minutes, × 0.9.
- `academic`: 3 kW at night; 25 kW between 09:00 and 17:00.
- All three × seeded noise `N(1, 0.05)`.
- `pump`: flexible. Needs `energy_kwh` per day inside `window`, at power ≤ `p_kw`.
- `heater`: flexible. Needs `energy_kwh` inside the overnight window 22:00→07:00, at power ≤ `p_kw`.
- `ev`: one session per day. It arrives at `arrive` (20:00) needing `energy_kwh` by `deadline` (08:00 next day), at power ≤ `p_kw`.

### 6.4 Assets and physics (applies to every controller)
Controller output each step (`Control`):
```
served_frac[l] for inflexible loads (0..1)
p_flex[l] kW for pump, heater, ev
p_charge, p_discharge, p_dg, curtail  (kW)
```
The twin enforces:
1. Clip to all limits (battery power and SoC bounds, DG power, remaining tank fuel, flexible load windows and power).
2. Gate with the MCU **applied mask**: a load whose bit is 0 gets `served = 0`.
3. Grid availability (live mode): `grid_up = cable_present AND relay_closed AND NOT software_outage`. In bench mode, it comes from the seeded outage schedule plus injected events.
4. Energy balance: `pv_used + grid + dg + discharge = Σ served + charge`, where `pv_used = pv − curtail`.
   - The grid is the slack when up, capped at `import_limit_kw`.
   - If the balance can't close (grid down or over the cap), the twin **auto-sheds** in `shed_order_baseline` order for the baselines and in reverse priority for the others, and records it as unserved.
   - Surplus PV that can't be stored is curtailed automatically (no export).
5. SoC update: `soc += (eta_c × ch − dis/eta_d) × dt`.
6. Fuel: `tank −= dg × dt × litres_per_kwh`.
7. Record per-step metrics (§12).

`Twin.step` must assert energy-balance residual `< 1e-6` kW.

### 6.5 Events (`events.py`)
Each event has `{step, type, params, source}` and is appended to the session event log (used for replay).

| type | params | effect |
|---|---|---|
| `grid_outage` | `{on: bool}` | Software outage on/off. Also makes io command the relay to open |
| `cloud` | `{factor: 0.3, duration_steps: 8}` | Actual PV × factor for the duration. **Not** given to the forecaster |
| `ev_fleet` | `{kwh: 30, deadline_hour: 6}` | Adds an extra EV session arriving now |
| `battery_fault` | `{capacity_factor: 0.5, power_factor: 0.5}` | Permanently scales capacity and power limits (until reset) |
| `live_sky` | `{multiplier: 0..1}` | From BH1750. Scales actual PV while live sky is enabled |
| `heat` | `{multiplier: 0.8..1.4}` | From BMP280. Scales hostel demand while enabled |

Physical grid-cable changes (from MCU status) are also logged as `grid_outage` events with `source: "cable"`.

---

## 7. Controllers

All controllers implement `decide(state, forecast_or_truth) -> Control` and run against the same twin.

### 7.1 B0: inverter default (self-consumption)
- Inflexible loads: request full service.
- Flexible loads: pump runs at `p_kw` from `b0_start` until its energy is met. Heater runs from `b0_start` (04:00) until met. EV charges at max from arrival until met.
- PV goes to loads first; surplus charges the battery; the rest is curtailed.
- Grid covers any deficit when up. **The battery discharges only when the grid is down** (backup mode).
- During an outage, the DG starts when SoC < 20% and supplies the remaining deficit.
- Shedding follows the fixed `shed_order_baseline` (no fairness).

### 7.2 B1: smart ToD rules (what a careful facility manager would program)
- Pump runs from 10:00 until met. Heater runs from 03:00 until met.
- EV charges only between 23:00 and 06:00 or on PV surplus, **except** it charges at max whenever `remaining_kwh / hours_left ≥ 0.9 × p_kw` (deadline guard).
- Battery: charges from PV surplus. Discharges during peak (18:00–23:00) down to a 40% reserve. During an outage it discharges down to `soc_min`.
- DG starts during an outage when SoC < 25%.
- Fixed shed order.

### 7.3 SAFE controller (used by the twin whenever the MCU reports SAFE)
- Serve only loads in the applied safe mask (default: clinic, pump), at full power.
- Battery discharges for any deficit if the grid is down; otherwise the grid covers it. DG starts if SoC < 20%.

### 7.4 GridQ MPC
Each step: build the LP (§8) with **forecasts** (§7.6), solve, and apply only the first step. If the solve fails or times out, keep using the previous plan's next step (plans carry the full horizon). Every solve records `solve_ms`.

### 7.5 Oracle
The same LP as the MPC, given the **true** future (demand, PV including cloud events, outage schedule). Benchmark only; labelled as the upper bound.

### 7.6 Forecasts
**Demand (the only machine-learning component):**
- **Models:** one P50 and one P90 model per I-BLEND feeder (hostel_a, hostel_b, academic). Use scikit-learn `HistGradientBoostingRegressor(loss="quantile", quantile=0.5 / 0.9)`.
- **Features** (`features.py`): hour and quarter-hour (sin/cos), weekday, weekend flag, lag 96 (same time yesterday), lag 672 (same time last week), lags 1–4, rolling 4-step and 96-step means. Optionally add the same-day NASA POWER temperature if the export includes `T2M`.
- **Training:** `python -m gridq.forecast.train` runs on the laptop, on the training period only. It takes seconds. It saves `data/models/*.joblib` plus a `metrics.json`, which is copied to the board. Inference on the board predicts 96 steps ahead recursively (or direct per-horizon if simpler) in well under 100 ms.
- **Baseline and fallback:** seasonal naive (same time yesterday). The model must beat seasonal naive on held-out days, or GridQ falls back to it and the dashboard says so.
- **Clinic forecast:** its modelled profile, with P90 = profile × 1.1.
- **How the outputs are used:** P50 feeds the LP's energy balance. P90 sizes the clinic reserve and the survival time. This is the uncertainty-aware step Panda et al. 2024 found beats single-value forecasts.
- **Solar:** `pv_clear_kw[t] × k_est`, where `k_est` is the EWMA (α = 0.5) of `actual/clear` over the last 4 steps with `clear > 1 kW`. It is initialised to the previous day's mean clearness.
- **Grid:** up for the whole horizon, except during an outage, when it is assumed down for the next `outage_assumed_remaining_steps`.
- **Benchmark report:**
  - Demand: P50 MAE vs seasonal naive, P90 pinball loss, and P90 coverage (the share of actuals ≤ P90; target ≈ 90%), all on held-out days.
  - Solar: MAE vs persistence.

---

## 8. LP formulation (`control/lp.py`)

The LP is pure, with no binaries, so it yields dual values. Build it with **CVXPY**, using the HiGHS or Clarabel solver. Fallback: `scipy.optimize.linprog(method="highs")`.

Horizon `T = horizon_steps`, `t = 0..T-1`, `dt = 0.25`.

**Variables (all ≥ 0 unless noted):**
`grid[t], ch[t], dis[t], dg[t], curt[t], soc[0..T]` (free, bounded), `shed[l,t]` for inflexible l, `p[f,t]` for flexible and session loads f, `slack[f]` (unmet flexible energy), `gap[t]` (reserve shortfall), `peak`.

**Objective (minimise):**
```
Σt tariff[t]·grid[t]·dt
+ Σt (rs_per_litre·litres_per_kwh)·dg[t]·dt
+ Σt wear·(ch[t]+dis[t])·dt
+ Σt Σl voll[l]·burden[l]·shed[l,t]·dt          # burden = 1 for non-fair loads
+ Σf slack_rs[f]·slack[f]
+ Σt curtail_penalty·curt[t]·dt
+ reserve_penalty·Σt gap[t]
+ peak_charge·peak
− terminal_value·soc[T]
```

**Constraints:**
```
balance[t]:  (pv_fc[t] − curt[t]) + grid[t] + dg[t] + dis[t]
             == Σl (dem_fc[l,t] − shed[l,t]) + Σf p[f,t] + ch[t]
curt[t] ≤ pv_fc[t]
shed[l,t] ≤ dem_fc[l,t]
grid[t] ≤ import_limit · grid_up_fc[t]
peak ≥ grid[t]
soc[0] = soc_now
soc[t+1] = soc[t] + (eta_c·ch[t] − dis[t]/eta_d)·dt
soc_min ≤ soc[t] ≤ soc_max          (kWh, using current fault-scaled capacity)
ch[t] ≤ p_ch_max, dis[t] ≤ p_dis_max (fault-scaled)
dg[t] ≤ dg_p_max;  Σt dg[t]·dt·litres_per_kwh ≤ tank_now
p[f,t] ≤ p_max_f · window_f[t]      (window/session availability mask)
Σ_{t < deadline_f} p[f,t]·dt + slack[f] ≥ remaining_energy_f
soc[t] + gap[t] ≥ clinic_p90_mean · reserve_hours_clinic   (t ≥ 1)   # P90 → uncertainty-aware reserve
```

**Plan output** (`Plan`): `step`, the full horizon arrays, `served_frac[l]` and `p_flex[f]` for `t = 0`, `safe_mask`, `solve_ms`, `objective`, `shadow_price_rs_per_kwh = |dual(balance[0])| / dt`, and the list of binding constraints at `t = 0` (SoC at min/max, grid at cap, `gap > 0`).

**Fairness:** `burden[l] = 1 + fairness_burden_per_hour × shed_minutes_so_far[l]/60` for loads with `fair: true`. It is updated every step from twin history (resets each day), so shedding rotates between hostels.

**Load mask from a plan:** bit = 1 if `served_frac > 0.05` (inflexible) or `p > 0.05·p_max` (flexible). Bit order is in §10.2.

---

## 9. Physical outputs and inputs

### 9.1 MCU state machine (`sketch/sketch.ino`)
- **NORMAL:** apply the mask from `set_outputs`. LED3 green, or amber if `led_state == 'C'` (constrained).
- **SAFE:** entered when no *new* heartbeat value has arrived for `heartbeat_timeout_ms`. Apply the last received `safe_mask`. LED3 red. The matrix alternates every 1 s between a "SAFE" glyph and the safe-mask bars. The buzzer gives one long tone (800 ms) on entry.
- Return to NORMAL on the next new heartbeat value, with a double chirp (2 × 80 ms).
- **Local grid trip (MCU-only, no Linux needed):** `cable_present = (debounced D3 == LOW)`, debounced 50 ms.
  - The relay is **energised (islanded)** immediately if `!cable_present OR grid_open_cmd == 1`.
  - The relay de-energises (recloses) only after `cable_present AND grid_open_cmd == 0` has held for `grid_reclose_delay_ms` (5 s).
  - Chirp 150 ms on every relay change.
- Boot: write the relay pin to its OFF level *before* `pinMode(OUTPUT)`. Start in SAFE until the first heartbeat.
- No blocking `delay()` in `loop()`. Use `millis()` timers for the buzzer, matrix blink and debounce.

### 9.2 LED matrix layout (8 rows × 13 columns; bar height = round(8 × fraction))

| Col | Meaning | Fraction |
|---|---|---|
| 0 | Clinic | served / demand |
| 1 | Pump | p / p_max |
| 2 | Hostel A | served / demand |
| 3 | Hostel B | served / demand |
| 4 | Academic | served / demand |
| 5 | EV | p / p_max |
| 6 | Heater | p / p_max |
| 7 | (blank separator) | 0 |
| 8 | Solar | pv / kwp |
| 9 | Battery SoC | soc / capacity |
| 10 | Grid import | grid / import_limit (0 when islanded) |
| 11 | Diesel | dg / dg_p_max |
| 12 | Heartbeat | toggles full/empty each new heartbeat (visible "pulse") |

Check the row/column orientation against the App Lab matrix example before finalising.

### 9.3 Status LEDs and buzzer
- **LED3:** green = NORMAL, amber (R+G) = CONSTRAINED (anything shed or grid down), red = SAFE.
- **LED4:** green = grid connected, blue = islanded.
- **Buzzer:** relay change 150 ms · SAFE entry 800 ms · SAFE exit 2 × 80 ms · EV arrival 60 ms.

### 9.4 ESP32 clinic node (`esp32/clinic_node/clinic_node.ino`)
- Wi-Fi STA on the 2.4 GHz hotspot. Auto-reconnect.
- Every 1 s: `GET http://GRIDQ_HOST:8000/api/node/clinic` (timeout 1.5 s). Parse with ArduinoJson.
- **LCD:**
  - line 1 = `l1`
  - line 2 cycles through the `l2` array every 3 s
  - all strings ≤ 16 ASCII chars, pre-formatted by the server (use `Rs` because the LCD has no ₹ glyph)
- **Servo:** target = `map(soc, 0..1 → servo_min..servo_max)`. Slew-limit to 3° per 50 ms.
- **Ultrasonic:**
  - Read every 200 ms (`pulseIn` with a 30 ms timeout; `cm = µs/58`).
  - When `ev_consecutive` readings in a row are under `ev_threshold_cm`, on that rising edge, `POST /api/events {"type":"ev_fleet","source":"esp32"}`.
  - Cooldown `ev_cooldown_s`.
- **Sensor:** if `bh1750`, POST `{"kind":"lux","value":x}` to `/api/sensor` every 2 s. If `bmp280`/`bme280`, POST `{"kind":"temp_c","value":x}`.
- **Link lost:** after 5 consecutive failed polls, the LCD shows `LINK LOST` / `CLINIC LOCAL ON`, GPIO2 blinks fast (5 Hz), and the servo holds its position. Recover automatically.

**Server-side sensor handling (io):**
- **Lux:** `lux_ref` = max of the first 10 readings after enabling live sky (or `POST /api/sensor/calibrate`). Multiplier = `clamp(lux/lux_ref, 0, 1)`. Applied only while `live_sky` is enabled in the dashboard. Logged as `live_sky` events whenever it changes by more than 0.05.
- **Temperature:** `heat` multiplier = `clamp(1 + 0.04 × (T − T_ref), 0.8, 1.4)`, with `T_ref` set at enable time.

---

## 10. Interfaces (do not change without team agreement)

### 10.1 Bridge RPC (MCU provides, io calls). Use **string payloads** for robustness.

| Function | Arg | Returns | Called |
|---|---|---|---|
| `hb` | `String` brain heartbeat counter, e.g. `"1532"` | `"ok"` | by io, **only** when a new heartbeat arrives from the brain (≈ every 500 ms) |
| `set_outputs` | `String` CSV: `mask,safe_mask,grid_open_cmd,cols,led_state` e.g. `"127,3,0,8853021000046,N"` | `"ok"` | by io every sim step. `cols` = 13 digits 0–8 per §9.2 (col 12 is overridden by the MCU's heartbeat pulse). `led_state` ∈ `N` (normal), `C` (constrained) |
| `get_status` | — | `String` CSV: `mode,applied_mask,cable_present,relay_energised,uptime_ms,last_hb` e.g. `"N,127,1,0,53211,1532"` (`mode` ∈ `N`/`S`) | by io every sim step, before twin.step |

io must **never** invent heartbeat values. The heartbeat originates in the brain process, so killing the brain genuinely starves the MCU watchdog.

If plain `Bridge.provide` calls hang when touching pins, use `Bridge.provide_safe`. Confirm all names against the App Lab Bridge example on the board.

### 10.2 Load bit order (mask)
`bit0 clinic, bit1 pump, bit2 hostel_a, bit3 hostel_b, bit4 academic, bit5 ev, bit6 heater` (all on = 127; safe default = clinic + pump = 3).

### 10.3 Brain ↔ io (localhost HTTP)
- `GET /internal/state?after_step=N` → long-poll (≤ 2 s) returning the `State` once `step > N`. The `State` includes current twin state, history needed for forecasts, active events, fault scaling, fuel and `shed_minutes`.
- `POST /internal/plan` ← `Plan` (§8).
- `POST /internal/heartbeat` ← `{"hb": int}` every 500 ms from a brain thread independent of solving.
- `POST /internal/explain` ← `{"step": N, "items": [Explanation]}` (§11).

### 10.4 Public API (io, port 8000)

| Method & path | Purpose |
|---|---|
| `GET /` | dashboard (static `web/`) |
| `GET /api/state` | latest full state + plan summary + mode + explanations |
| `GET /api/stream` | SSE: one `state` event per sim step |
| `POST /api/events` | `{type, params?, source?}` inject an event (§6.5) |
| `POST /api/sim/{play,pause,reset}` · `POST /api/sim/speed {x}` | clock control. `reset` restores the demo start state and clears the session log |
| `POST /api/demo/kill-brain` | SIGKILL the brain process. io respawns it after `brain_restart_delay_s` |
| `POST /api/live/{sky,heat}` `{enabled: bool}` | toggle sensor-driven inputs |
| `GET /api/node/clinic` | `{"l1": str, "l2": [str, ...], "soc": float, "mode": "N"/"S", "clinic_on": bool, "seq": int}` |
| `POST /api/sensor` · `POST /api/sensor/calibrate` | ESP32 sensor input |
| `GET /api/bench` | contents of `data/bench/results.json` |
| `POST /api/replay` | replay the current session (snapshot + event log) through B0 and GridQ, headless. Returns both metric sets |

**LCD formatting** (`node_format.py`), 16 chars max:
- `l1`: `GRID OK   HH:MM` or `GRID DOWN HH:MM` or `SAFE MODE HH:MM`
- `l2[0]`: `SECURE 11h20m` (survival time; `>48h` if over 48)
- `l2[1]`: top explanation, e.g. `EV paused +Rs41`
- `l2[2]`: `BATT 62% PV 18kW`

---

## 11. Explanations (`control/explain.py`, runs in the brain after each main solve)

- **Shadow price:** `|dual(balance[0])| / dt`, shown as "Energy is worth ₹X/kWh right now".
- **Counterfactuals:** for up to 3 decisions at `t = 0`, ranked by the size of the deferral or shed:
  - **Deferred flexible load** (p[0] = 0 while energy remains and the window is open): re-solve with `p[f,0] = p_max`.
  - **Shed inflexible load** (shed[l,0] > 0): re-solve with `shed[l,0] = 0`.
  - Report `delta_rs = obj_cf − obj_base`. Also report the finish time of flexible energy in the base plan, and whether the deadline is met.
  - Text template: `"<Load> paused. Running it now costs ₹<delta> more. Deadline <hh:mm> still met (finishes <hh:mm>)."`
- **Binding constraints:** add one line per binding constraint, e.g. "Battery at reserve floor", "Grid at 40 kW limit".
- **Survival time** (shown as "Clinic secure for Xh Ym"):
  - Simulate forward with the grid down, serving only priority-1 loads at their **P90** forecast demand (conservative).
  - Use forecast PV, then battery down to `soc_min`, then DG until the tank is empty.
  - Stop at the first unmet step. Cap at 48 h.
- Time budget: 2 s per step. Skip counterfactuals if the main solve took > 1.5 s.

---

## 12. Metrics (`bench/metrics.py`), per day and aggregated

| Metric | Definition |
|---|---|
| `cost_rs` | Σ tariff·grid·dt + Σ dg·dt·litres_per_kwh·rs_per_litre (headline cost; wear reported separately) |
| `battery_wear_rs` | Σ wear·(ch+dis)·dt |
| `critical_uptime_pct` | % of steps where clinic served ≥ 99% of demand |
| `critical_unserved_kwh` | Σ clinic unserved energy |
| `unserved_kwh_total` | Σ all inflexible unserved + flexible energy missed at deadlines |
| `deadline_misses` | count of flexible sessions not completed by their deadline |
| `diesel_l` | Σ dg·dt·litres_per_kwh |
| `co2_kg` | diesel_l·2.68 + Σ grid·dt·0.71 |
| `peak_grid_kw` | max grid |
| `solar_self_use_pct` | Σ(pv − curt) / Σ pv |
| `shed_minutes[l]` and `fairness_jain` | Jain index over hostel_a, hostel_b, academic shed minutes (1.0 = perfectly even; define as 1.0 if all zero) |
| `captured_pct` | (B1.cost − GridQ.cost) / (B1.cost − Oracle.cost), on report days |
| `solve_ms` | median and p95 per controller (MPC and Oracle) |
| `forecast` | demand: P50 MAE vs seasonal naive, P90 pinball loss, P90 coverage %; PV: MAE vs persistence (held-out days only) |

**Benchmark protocol** (`python -m gridq.bench.run_days`):
- Run B0, B1, GridQ and Oracle over the `bench.days` **held-out real I-BLEND days** × `outage_seeds` outage patterns (150 scenario-days). Every controller gets identical inputs.
- Weights may only be tuned using `tune_days`. Report medians, 10th–90th percentile ranges and min–max over `report_days` only (100 scenario-days).
- Show the worst scenario-day for GridQ alongside the median.
- Write `data/bench/results.json` with per-day and aggregate values plus run metadata: git hash, params hash, machine (`uname -a`) and date.
- It can be precomputed on a laptop. The same command must also run on the UNO Q.

---

## 13. Live loop (io sim clock thread, once per sim step)

1. `status = bridge.get_status()`.
2. Drain queued events (API, ESP32, sensor, cable changes from status) into the twin and the session log.
3. Choose control:
   - If `status.mode == 'S'`, use the SAFE controller.
   - Otherwise use the latest brain plan at offset `k − plan.step` (valid up to 8 steps old).
   - Otherwise fall back to the SAFE controller.
4. `result = twin.step(control, applied_mask = status.applied_mask, grid_up = cable && !relay_energised && !software_outage)`.
5. Build `cols` and `led_state`, then call `bridge.set_outputs(...)` with the plan mask (or 127 if no plan), the plan's `safe_mask`, and `grid_open_cmd = software_outage`.
6. Broadcast the SSE `state`.

A separate thread forwards each new brain heartbeat via `bridge.hb()`.

The brain supervisor spawns `python -m gridq.brain.main` as a child process. Respawn it on exit after `brain_restart_delay_s`. `kill-brain` sends SIGKILL.

---

## 14. Dashboard (`web/`, plain HTML/JS, vendored uPlot, works with no internet)

Mobile-first, single page, five tabs. The permanent header shows:
- the `SIMULATED PLANT · REAL CONTROLLER` badge
- the sim clock and tariff band (solar / normal / peak)
- the mode pill (NORMAL / CONSTRAINED / SAFE)
- grid status
- the brain status (alive, or restarting with a countdown)
- `solve_ms`

1. **Live:**
   - Energy-flow summary: Solar, Grid, Battery (SoC bar), Diesel → Bus → 7 feeder chips, each showing ON / PARTIAL x% / DEFERRED / SHED.
   - "Clinic secure for Xh Ym".
   - Energy price now (shadow price).
2. **Plan:**
   - 24 h stacked chart of planned supply (PV used, grid, battery, DG) vs demand, plus a SoC line.
   - Why cards (§11).
3. **Break it:** big buttons for Grid outage (toggle), Cloud cover, E-rickshaw fleet, Battery fault, Kill the brain; Live sky and Heat toggles; a sim speed and pause control; Reset.
4. **Proof:**
   - Table of B0 / B1 / GridQ / Oracle on report days: cost, critical uptime, diesel L and CO₂, peak kW, solar self-use, fairness. Show medians with min–max.
   - The `captured_pct` headline.
   - Per-day cost chart.
   - Provenance line, read from `demand_meta.json` and results.json, e.g. "Demand: I-BLEND (IIIT-Delhi), held-out days · Solar: NASA POWER · 100 scenario-days · method after Panda et al. 2024".
   - Forecast card: P50 error vs "same as yesterday", and P90 coverage.
5. **Replay:** a "Replay this session against the baseline" button. Shows B0 vs GridQ side by side for the judge's own session.

Visual rule: feeder states use colour plus text (never colour alone).

---

## 15. Deployment on the UNO Q

- App Lab app with `python/` (entry starts `gridq.io.app` with uvicorn on `0.0.0.0:8000`) and `sketch/`.
- Python deps: `numpy pandas pyyaml cvxpy highspy fastapi uvicorn scikit-learn joblib`. Add them to the app's requirements mechanism.
- Model training and data preparation happen on the laptop. Copy `data/` (demand CSV, solar CSV, models, bench results) to the board. Pin the same scikit-learn version on both so the joblib files load.
- **Verify early:**
  - Port 8000 is reachable from a phone on the hotspot. If App Lab's container blocks it, use App Lab's documented web/port mechanism.
  - The brain subprocess can be spawned inside the app.
- Everything is offline: no CDN, no external API calls at runtime. `fetch_nasa_power.py` runs once beforehand.
- Logs go to stdout plus `data/session_*.jsonl`.

---

## 16. Tests and acceptance criteria

- `test_twin.py`: energy balance residual < 1e-6 on every step of 3 seeded days for every controller; SoC and fuel stay within bounds.
- `test_determinism.py`: the same seed and events give byte-identical metrics for all controllers. Replay of a recorded session reproduces the live metrics.
- `test_lp.py`: status optimal on all 30 days. Oracle cost ≤ GridQ cost on every report day. Duals are present. A 96-step solve completes in < 1 s on the laptop (log the board figure).
- `test_metrics.py`: hand-computed tiny cases for cost, uptime, Jain index and `captured_pct`.
- `test_forecast.py`:
  - No benchmark date appears in the training set.
  - P50 MAE < seasonal-naive MAE on held-out days (if this fails, the fallback flag must be set).
  - P90 coverage is between 80% and 97%.
  - Features at inference use no future data.

**Hardware acceptance (manual):**
1. Pulling the grid cable clicks the relay within 100 ms with no Linux involvement (test with the brain killed).
2. Re-inserting it recloses after 5 s.
3. Kill-brain causes SAFE within 3.5 s (LED3 red, long tone), and NORMAL resumes after respawn.
4. The ESP32 LCD reflects outage within 2 s.
5. The ultrasonic triggers `ev_fleet` exactly once per approach.
6. Stopping io makes the ESP32 show LINK LOST within 6 s.

---

## 17. Build order (phases and gates)

| Phase | Deliverable | Gate |
|---|---|---|
| P1 (H0–H5) | **First:** download I-BLEND, run `prepare_iblend.py` + `fetch_nasa_power.py` (laptop, internet). Then `params.yaml`, `profiles.py`, `twin.py`, B0, `metrics.py`, `lp.py` + Oracle on 1 day, MockBridge | B0 and Oracle print cost and unserved energy for 1 day on the laptop |
| P1-HW (parallel) | `sketch.ino`: relay, cable, buzzer, matrix bars, LEDs, `get_status` / `set_outputs` / `hb`, SAFE watchdog | Hardware acceptance items 1–3 pass using a tiny Python test script |
| P2 (H5–H10) | Quantile demand forecaster trained on I-BLEND (laptop) + solar forecaster, MPC rolling loop, B1, events, `run_days.py`; io app with SSE, brain process, real Bridge | MPC runs live on the UNO Q, the phone dashboard updates, pulling the cable clicks the relay |
| P2-HW (parallel) | ESP32 `clinic_node.ino`: LCD, servo, ultrasonic, poll + POST, link-lost | Hardware acceptance items 4–6 |
| P3 (H10–H14) | Explanations, fairness burden, survival time, replay, Proof + Plan tabs | 30-day results.json exists; replay works |
| P4 (H14–H17) | Optional: live sky / heat sensor; polish | **Feature freeze at H17** |
| P5 | Bug fixes only, backup video, rehearsal | Two clean demo runs in a row |

If behind at a gate, cut in this order: live sensor → replay → fairness → explanation counterfactuals (keep the shadow price) → P90 model (use P50 × 1.2) → B1.

If I-BLEND download or parsing takes longer than 90 minutes, switch to the synthetic fallback profiles, keep going, and retry I-BLEND in P4. **Never cut:** the twin, the LP, the Oracle, B0, the benchmark, the MCU SAFE watchdog, or the honesty badge.

---

## 18. Non-goals (do not build)

CNNs or any deep neural network (the optimizer is exact optimisation, and the only ML is the small quantile forecaster), reinforcement learning (roadmap only, see §20), P2P trading, energy markets, NILM, cloud services, Firebase or Flutter, Docker microservices, LLM features, electrochemical battery models, MILP, React build pipelines, AC power-flow or voltage modelling, anything touching mains.

---

## 19. Open items to verify on the real hardware (record the answers in `config/hardware.yaml`)

1. Does the relay trigger from 3.3 V? Is it active-LOW? Which terminal is NC?
2. Is the buzzer active or passive?
3. Exact Bridge API names (`Bridge.provide`, `provide_safe`, `Bridge.call`, `App.run`) for the installed App Lab version.
4. LED matrix draw API and orientation; LED3/LED4 pin names.
5. LCD I2C address; the blue module's identity (by address).
6. Usable servo angle range.
7. UNO Q RAM and LP solve time on the board.
8. Whether port 8000 is reachable from a phone through App Lab.
9. I-BLEND file layout: building names, units (W or kW), timezone, sampling interval, gaps and date range. Record the result in `demand_meta.json`.

---

## 20. References and what we claim from them

1. **Panda, S. P., Genest, B., Easwaran, A., Rigo-Mariani, R., & Lin, P. (2024). "Methods for mitigating uncertainty in real-time operations of a connected microgrid."** *Sustainable Energy, Grids and Networks* (DOI 10.1016/j.segan.2024.101334); arXiv:2409.19568.
   - **Their study:** they compare a rule-based EMS, MPC with deterministic and with stochastic forecasts, and deep RL on real microgrid data, all benchmarked against a perfect-foresight controller.
   - **Finding 1:** MPC with an uncertainty-aware forecast beats MPC with a single-value forecast.
   - **Finding 2:** deep RL beats the best MPC by about 1.3%.
   - **What we take:** the evaluation design (baseline vs MPC vs perfect-foresight bound), and the use of uncertainty (our P90 reserve).
   - **What we claim:** we follow their evaluation method and apply it to Indian campus data on a low-cost edge board. MPC was chosen over RL because it needs no training, explains every decision, and comes within a small margin of RL in their results.
2. **Rashid, H., Singh, P., & Singh, A. (2019). "I-BLEND, a campus-scale commercial and residential buildings electrical energy dataset."** *Scientific Data* 6, 190015. Data on figshare: https://springernature.figshare.com/collections/I-BLEND_a_campus_scale_commercial_and_residential_buildings_electrical_energy_dataset/3893581
   - **What we use:** real hostel and academic demand, for forecaster training and as held-out benchmark days.
3. **NASA POWER** hourly solar irradiance (https://power.larc.nasa.gov).
4. **Ministry of Power, Electricity (Rights of Consumers) Amendment Rules, 2023.** These set Time-of-Day tariffs: solar hours 10–20% cheaper, peak 10–20% dearer.

**Never claim:** a novel algorithm, a real deployment, or that simulated savings will transfer to a real site. **Do claim:** a real controller running on edge hardware, real Indian demand and solar data, a held-out benchmark, and computed (not invented) explanations.
