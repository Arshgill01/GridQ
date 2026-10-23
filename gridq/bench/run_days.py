"""Benchmark CLI: B0, B1, GridQ and Oracle over held-out real days x outage seeds.

    python -m gridq.bench.run_days [--days 30] [--seeds 5] [--controllers B0,B1,GridQ,Oracle]

Protocol (spec §12, DECISIONS.md D1/D4): for each outage seed, every controller
runs the same continuous multi-day simulation from the same initial state, so the
battery carries over between days and nobody gains by draining it overnight. The
end-of-run battery energy is settled at `bench.settle_rs_per_kwh`. Per-day
metrics are reported; weights may only be tuned on `tune_days`.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np

from gridq.bench import metrics as M
from gridq.common.config import ROOT, SPD, load_params, params_hash
from gridq.plant.outages import grid_schedule
from gridq.plant.profiles import load_dataset
from gridq.plant.twin import Timeline, Twin

CONTROLLERS = ["B0", "B1", "GridQ", "Oracle"]


def make_controller(name: str, p: dict):
    from gridq.control.baseline_b0 import B0
    from gridq.control.baseline_b1 import B1
    from gridq.control.mpc import MPC, Oracle
    return {"B0": B0, "B1": B1, "GridQ": MPC, "Oracle": Oracle}[name](p)


def bench_days(p: dict, ds, n_days: int):
    test = ds.test_days()[:n_days]
    first = ds.days.index(test[0])
    pre = ds.days[max(0, first - 7):first]
    nxt = ds.days[ds.days.index(test[-1]) + 1] if ds.days.index(test[-1]) + 1 < len(ds.days) else test[-1]
    return test, pre, nxt


def run_one(args):
    name, seed, n_days, overrides = args
    p = load_params(overrides=overrides)
    ds = load_dataset(p)
    test, pre, nxt = bench_days(p, ds, n_days)
    sched = grid_schedule(p, len(test), p["bench"]["seed_base"] + seed)
    tl = Timeline.from_days(p, test + [nxt], n_sim=len(test), grid_sched=sched, pre_days=pre)
    tw = Twin(p, tl)
    ctrl = make_controller(name, p)
    t0 = time.perf_counter()
    for _ in range(tl.n_steps):
        obs = tw.observe()
        c = ctrl.decide(obs, tw)
        tw.step(c)
    wall = time.perf_counter() - t0
    days = []
    for d in range(len(test)):
        m = M.day_metrics(p, tw.rec, d)
        m["date"] = test[d].date
        m["day"] = d + 1
        m["seed"] = seed
        days.append(m)
    st = M.settle(p, tw.soc_start, tw.soc)
    days[-1]["settle_rs"] = st
    days[-1]["total_cost_rs"] += st
    ms = getattr(ctrl, "solve_ms", [])
    return {"controller": name, "seed": seed, "days": days, "wall_s": wall,
            "solve_ms": [float(np.median(ms)), float(np.percentile(ms, 95))] if ms else None,
            "solve_fail": getattr(ctrl, "fail", 0), "max_residual": tw.max_residual,
            "fallback": ds.fallback}


def git_hash() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def run(controllers=CONTROLLERS, n_days=None, seeds=None, overrides=None, workers=None, out=None, quiet=False):
    p = load_params(overrides=overrides)
    n_days = n_days or p["bench"]["days"]
    seeds = list(range(seeds if seeds is not None else p["bench"]["outage_seeds"]))
    jobs = [(c, s, n_days, overrides) for c in controllers for s in seeds]
    workers = workers or min(len(jobs), os.cpu_count() or 1)
    t0 = time.time()
    if workers > 1:
        with ProcessPoolExecutor(workers) as ex:
            results = list(ex.map(run_one, jobs))
    else:
        results = [run_one(j) for j in jobs]
    rep = report(p, results, controllers)
    rep["meta"] = {"git": git_hash(), "params_hash": params_hash(p), "machine": " ".join(platform.uname()),
                   "date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "wall_s": time.time() - t0,
                   "n_days": n_days, "seeds": len(seeds), "overrides": overrides or {}}
    ds = load_dataset(p)
    rep["meta"]["fallback"] = ds.fallback
    rep["meta"]["data"] = ds.meta
    rep["meta"]["dates"] = [d.date for d in bench_days(p, ds, n_days)[0]]
    if out:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(rep, f, indent=1)
    if not quiet:
        print_report(rep)
    return rep


def report(p, results, controllers):
    t0, t1 = p["bench"]["tune_days"]
    r0, r1 = p["bench"]["report_days"]
    per = {c: [] for c in controllers}
    extra = {c: {"solve_ms": [], "wall_s": [], "solve_fail": 0, "max_residual": 0.0} for c in controllers}
    for res in results:
        per[res["controller"]].extend(res["days"])
        e = extra[res["controller"]]
        if res["solve_ms"]:
            e["solve_ms"].append(res["solve_ms"])
        e["wall_s"].append(res["wall_s"])
        e["solve_fail"] += res["solve_fail"]
        e["max_residual"] = max(e["max_residual"], res["max_residual"])
    out = {"per_day": per, "tune": {}, "report": {}, "controllers": controllers, "extra": {}}
    for c in controllers:
        tune = [d for d in per[c] if t0 <= d["day"] <= t1]
        rep = [d for d in per[c] if r0 <= d["day"] <= r1]
        out["tune"][c] = M.summarise(tune) if tune else None
        out["report"][c] = M.summarise(rep) if rep else None
        e = extra[c]
        out["extra"][c] = {"solve_ms_median": float(np.median([x[0] for x in e["solve_ms"]])) if e["solve_ms"] else None,
                           "solve_ms_p95": float(np.max([x[1] for x in e["solve_ms"]])) if e["solve_ms"] else None,
                           "solve_fail": e["solve_fail"], "max_residual": e["max_residual"],
                           "wall_s": float(np.sum(e["wall_s"]))}
    for split in ("tune", "report"):
        s = out[split]
        if all(s.get(c) for c in ("B1", "GridQ", "Oracle")):
            for key in ("total_cost_rs", "cost_rs"):
                out.setdefault("captured_pct", {})[f"{split}_{key}"] = M.captured_pct(
                    s["B1"][key]["sum"], s["GridQ"][key]["sum"], s["Oracle"][key]["sum"])
    if "GridQ" in per and "Oracle" in per:
        key = lambda d: (d["seed"], d["day"])
        o = {key(d): d["total_cost_rs"] for d in per["Oracle"] if r0 <= d["day"] <= r1}
        g = {key(d): d["total_cost_rs"] for d in per["GridQ"] if r0 <= d["day"] <= r1}
        common = [k for k in g if k in o]
        out["oracle_le_gridq"] = {"days": len(common), "exceptions": sum(1 for k in common if o[k] > g[k] + 1e-6),
                                  "note": "continuous runs: per-day order is not guaranteed, totals are"}
    if all(c in per for c in ("GridQ",)):
        rep = [d for d in per["GridQ"] if r0 <= d["day"] <= r1]
        if rep:
            worst = max(rep, key=lambda d: d["total_cost_rs"])
            out["worst_gridq"] = {k: worst[k] for k in ("date", "seed", "day", "total_cost_rs", "cost_rs",
                                                        "unserved_kwh_total", "outage_steps")}
    return out


def print_report(rep):
    keys = ["total_cost_rs", "cost_rs", "battery_wear_rs", "unserved_value_rs", "unserved_kwh_total",
            "critical_uptime_pct", "deadline_misses", "diesel_l", "peak_grid_kw", "solar_self_use_pct", "fairness_jain"]
    for split in ("tune", "report"):
        s = rep[split]
        if not any(s.values()):
            continue
        print(f"\n== {split} days (medians; sum for costs) ==")
        print(f"{'metric':22s}" + "".join(f"{c:>12s}" for c in rep["controllers"]))
        for k in keys:
            row = f"{k:22s}"
            for c in rep["controllers"]:
                v = s[c][k]["sum" if k.endswith("_rs") else "median"] if s[c] else float("nan")
                row += f"{v:12.1f}"
            print(row)
    if "captured_pct" in rep:
        print("captured:", {k: round(v, 1) for k, v in rep["captured_pct"].items()})
    for c, e in rep["extra"].items():
        print(c, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in e.items()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int)
    ap.add_argument("--seeds", type=int)
    ap.add_argument("--controllers", default=",".join(CONTROLLERS))
    ap.add_argument("--workers", type=int)
    ap.add_argument("--out", default=str(ROOT / "data" / "bench" / "results.json"))
    ap.add_argument("--set", action="append", default=[], help="override, e.g. mpc.scenarios.enabled=true")
    ap.add_argument("--fold", default=None, help="held-out block: winter (spec, default) | summer")
    a = ap.parse_args()
    ov = {}
    if a.fold:
        ov["bench"] = {"fold": a.fold}
        if a.out == str(ROOT / "data" / "bench" / "results.json") and a.fold != "winter":
            a.out = str(ROOT / "data" / "bench" / f"results_{a.fold}.json")
    for kv in a.set:
        k, v = kv.split("=", 1)
        import yaml
        cur = ov
        parts = k.split(".")
        for q in parts[:-1]:
            cur = cur.setdefault(q, {})
        cur[parts[-1]] = yaml.safe_load(v)
    run(a.controllers.split(","), a.days, a.seeds, ov, a.workers, a.out)


if __name__ == "__main__":
    main()
