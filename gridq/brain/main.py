"""gridq-brain: a separate, killable process (spec §3, §10.3).

    python -m gridq.brain.main [--io http://127.0.0.1:8000]

* heartbeat thread: POST /internal/heartbeat every 500 ms, independent of solving.
  This is the ONLY origin of heartbeat values; killing this process starves the
  MCU watchdog for real.
* main loop: long-poll /internal/state, forecast, solve the LP, POST the plan,
  then compute explanations and POST them.
"""
from __future__ import annotations

import argparse
import logging
import threading
import time

import json
import urllib.error
import urllib.request

import numpy as np

from gridq.common.config import DT, FLEX, INFLEX, SPD, load_params
from gridq.common.schema import BrainView
from gridq.control import explain as X
from gridq.control import lp
from gridq.control.mpc import MPC

log = logging.getLogger("gridq.brain")
NetErr = (urllib.error.URLError, OSError, TimeoutError)


def post(url: str, body: dict, timeout: float):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def get_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, (json.loads(r.read()) if r.status == 200 else None)
PLAN_STEPS = 9   # control arrays carried for t = 0..8 (io may use a plan up to 8 steps old)


def heartbeat(io: str, stop: threading.Event):
    n = 0
    while not stop.is_set():
        n += 1
        try:
            post(f"{io}/internal/heartbeat", {"hb": n}, timeout=0.4)
        except NetErr:
            pass
        stop.wait(0.5)


def plan_payload(p, k, data: lp.LPData, res: lp.LPResult, fc: dict, mpc: MPC) -> dict:
    tr = res.trunk
    n = min(PLAN_STEPS, data.T)
    served = {}
    for l in INFLEX:
        d = np.maximum(data.dem[l][:n], 1e-9)
        served[l] = np.clip(1 - tr[f"shed_{l}"][:n] / d, 0, 1).round(4).tolist()
    pv_used = data.pv - tr["curt"]
    load = sum(data.dem[l] - tr[f"shed_{l}"] for l in INFLEX) + sum(tr[f"p_{f}"] for f in FLEX)
    return {
        "step": k, "solve_ms": res.solve_ms, "objective": res.objective, "shadow_price": res.shadow_price,
        "binding": res.binding, "n_vars": res.n_vars, "n_branches": len(data.branches),
        "forecaster": getattr(mpc.fc, "name", "?"), "forecast_fallback": bool(getattr(mpc.fc, "fallback", False)),
        "ctrl": {"served_frac": served, "p_flex": {f: tr[f"p_{f}"][:n].round(4).tolist() for f in FLEX},
                 "ch": tr["ch"][:n].round(4).tolist(), "dis": tr["dis"][:n].round(4).tolist(),
                 "dg": tr["dg"][:n].round(4).tolist()},
        "horizon": {"pv_used": pv_used.round(3).tolist(), "grid": tr["grid"].round(3).tolist(),
                    "dis": tr["dis"].round(3).tolist(), "ch": tr["ch"].round(3).tolist(),
                    "dg": tr["dg"].round(3).tolist(), "load": load.round(3).tolist(),
                    "soc": tr["soc"].round(3).tolist(), "pv_fc": data.pv.round(3).tolist(),
                    "dem_p50": sum(fc["p50"][l] for l in INFLEX).round(3).tolist(),
                    "clinic_p90": fc["p90"]["clinic"].round(3).tolist(),
                    **{f"p_{f}": tr[f"p_{f}"].round(3).tolist() for f in FLEX},
                    **{f"shed_{l}": tr[f"shed_{l}"].round(3).tolist() for l in INFLEX}},
    }


def run(io: str):
    p = load_params()
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(io, stop), daemon=True).start()
    mpc = MPC(p)
    last = -1
    log.info("brain up: forecaster=%s", getattr(mpc.fc, "name", "?"))
    while True:
        try:
            code, st = get_json(f"{io}/internal/state?after_step={last}", timeout=4)
        except NetErr:
            time.sleep(0.5)
            continue
        if code != 200 or st is None:
            continue
        view = BrainView(st)
        obs = view.obs()
        k = obs.k
        last = k
        data, fc = mpc.build(obs, view)
        res = lp.solve(p, data, time_limit=p["mpc"]["solve_timeout_s"])
        if not res.ok:
            log.warning("solve failed at %d: %s", k, res.status)
            continue
        try:
            post(f"{io}/internal/plan", _clean(plan_payload(p, k, data, res, fc, mpc)), timeout=2)
        except NetErr:
            continue
        t0 = time.perf_counter()
        items = X.explain(p, data, res, k, budget_ok=res.solve_ms < 1500)
        try:
            post(f"{io}/internal/explain", {"step": k, "items": _clean(items),
                                            "explain_ms": (time.perf_counter() - t0) * 1000}, timeout=2)
        except NetErr:
            pass


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if x != x else float(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--io", default="http://127.0.0.1:8000")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s brain %(levelname)s %(message)s")
    run(a.io)


if __name__ == "__main__":
    main()
