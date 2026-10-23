"""Compare GridQ variants on the TUNE days only (days 1-10 x all outage seeds).

    python tools/tune.py            # writes data/bench/tune.json and prints a table

Report days (11-30) are never simulated here, so no tuning decision can see them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gridq.bench import run_days as R  # noqa: E402

VARIANTS = {
    "spec: P90 reserve, deterministic": {"mpc": {"reserve_mode": "p90", "scenarios": {"enabled": False}}},
    "P90 reserve released when islanded": {"mpc": {"reserve_mode": "p90_release", "scenarios": {"enabled": False}}},
    "no reserve, deterministic": {"mpc": {"reserve_mode": "none", "scenarios": {"enabled": False}}},
    "fan tree": {"mpc": {"reserve_mode": "none", "scenarios": {"enabled": True}}},
    "fan tree + P90 reserve": {"mpc": {"reserve_mode": "p90", "scenarios": {"enabled": True}}},
    "fan tree, 3 durations": {"mpc": {"reserve_mode": "none", "scenarios": {"enabled": True, "durations_steps": [2, 6, 11]}}},
    "fan tree, terminal value 8": {"mpc": {"reserve_mode": "none", "terminal_value_rs_per_kwh": 8.0, "scenarios": {"enabled": True}}},
    "fan tree, branches to 8 h": {"mpc": {"reserve_mode": "none", "scenarios": {"enabled": True, "branch_starts": [1, 2, 4, 8, 16, 32]}}},
    "fan tree, min-max fairness": {"mpc": {"reserve_mode": "none", "fairness": "minmax", "scenarios": {"enabled": True}}},
}


def main(names=None):
    tune_days = 10
    base = R.run(["B1", "Oracle"], n_days=tune_days, quiet=True)
    out = {"B1": base["tune"]["B1"], "Oracle": base["tune"]["Oracle"], "variants": {}}
    prev = ROOT / "data" / "bench" / "tune.json"
    if prev.exists() and names:      # keep earlier variants when re-running a subset
        out["variants"] = json.loads(prev.read_text()).get("variants", {})
    b1 = base["tune"]["B1"]["total_cost_rs"]["sum"]
    orc = base["tune"]["Oracle"]["total_cost_rs"]["sum"]
    print(f"{'variant':38s} {'total ₹':>10s} {'captured':>9s} {'unserved':>9s} {'wear ₹':>8s} {'jain':>5s} {'ms':>6s}")
    print(f"{'B1':38s} {b1:10.0f}")
    print(f"{'Oracle':38s} {orc:10.0f}")
    for name, ov in VARIANTS.items():
        if names and name not in names:
            continue
        r = R.run(["GridQ"], n_days=tune_days, overrides=ov, quiet=True)
        g = r["tune"]["GridQ"]
        tot = g["total_cost_rs"]["sum"]
        cap = 100 * (b1 - tot) / (b1 - orc)
        out["variants"][name] = {"overrides": ov, "summary": g, "captured_pct": cap, "extra": r["extra"]["GridQ"]}
        print(f"{name:38s} {tot:10.0f} {cap:8.1f}% {g['unserved_kwh_total']['sum']:9.1f} "
              f"{g['battery_wear_rs']['sum']:8.0f} {g['fairness_jain']['median']:5.2f} "
              f"{r['extra']['GridQ']['solve_ms_median']:6.1f}")
    meta = base["meta"]
    out["meta"] = meta
    (ROOT / "data" / "bench").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "bench" / "tune.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
