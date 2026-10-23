"""Replay a live session (snapshot + event log + grid availability) headless (spec §10.4 /api/replay).

* "live": the controls actually applied each step, re-run through a fresh twin.
  Reproduces the live metrics exactly (test_determinism).
* "B0" and "GridQ": the same session, same events and the same grid availability,
  decided by the inverter default and by GridQ in-process.
"""
from __future__ import annotations

import copy

from gridq.bench.metrics import range_metrics
from gridq.plant.events import Event
from gridq.plant.twin import Control


def _run(p, snapshot, session, decide):
    tw = copy.deepcopy(snapshot)
    evs = {}
    for e in session.get("events", []):
        if e.get("applied", True):
            evs.setdefault(e["step"], []).append(e)
    for st in session["steps"]:
        for e in evs.get(st["k"], []):
            tw.apply(Event(step=e["step"], type=e["type"], params=e["params"], source=e["source"]))
        obs = tw.observe(grid_up=st["grid_up"])
        c, mask, mode = decide(tw, obs, st)
        tw.step(c, applied_mask=mask, grid_up=st["grid_up"], mode=mode)
    a = session["start_step"]
    return range_metrics(p, tw.rec, a, tw.k)


def replay_session(p: dict, snapshot, session: dict, controllers=("live", "B0", "GridQ")) -> dict:
    if not session["steps"]:
        return {"error": "empty session: let the clock run first"}
    out = {"steps": len(session["steps"]), "start_step": session["start_step"], "date": session.get("date")}

    def live(tw, obs, st):
        c = st["ctrl"]
        return (Control(served_frac=c["served_frac"], p_flex=c["p_flex"], p_charge=c["ch"], p_discharge=c["dis"],
                        p_dg=c["dg"], shed_policy=c["policy"]), st["mask"], 1 if st["src"].startswith("SAFE") else 0)

    for name in controllers:
        if name == "live":
            out["live"] = _run(p, snapshot, session, live)
            continue
        from gridq.bench.run_days import make_controller
        ctrl = make_controller(name, p)
        out[name] = _run(p, snapshot, session, lambda tw, obs, st: (ctrl.decide(obs, tw), 127, 0))
    return out
