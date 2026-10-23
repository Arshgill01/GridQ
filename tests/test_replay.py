"""Replay of a recorded live session reproduces the live metrics (spec §16 test_determinism)."""
import json
import os

os.environ["GRIDQ_MOCK_BRIDGE"] = "1"
os.environ["GRIDQ_NO_BRAIN"] = "1"


def test_replay_reproduces_live(tmp_path):
    from gridq.bench.metrics import range_metrics
    from gridq.bench.replay import replay_session
    from gridq.io import app as A

    from gridq.brain.main import _clean, plan_payload
    from gridq.common.schema import BrainView
    from gridq.control import lp
    from gridq.control.mpc import MPC

    live = A.Live()
    live.session_path = tmp_path / "s.jsonl"
    mpc = MPC(A.P)
    hb = 0
    for i in range(20):
        if i < 12:                       # in-process "brain": plan from the same JSON state the real brain gets
            view = BrainView(json.loads(json.dumps(live.brain_state)))
            data, fc = mpc.build(view.obs(), view)
            res = lp.solve(A.P, data)
            live.plan = _clean(plan_payload(A.P, view.k, data, res, fc, mpc))
        if i < 12:                       # brain alive for a while, then "killed": SAFE mode kicks in on the mock MCU
            hb += 1
            live.bridge.hb(str(hb))
            live.bridge.last_hb_t -= 0   # keep fresh
        if i == 3:
            live.events_q.append({"type": "grid_outage", "params": {"on": True}})
        if i == 6:
            live.events_q.append({"type": "grid_outage", "params": {"on": False}})
        if i == 8:
            live.events_q.append({"type": "ev_fleet"})
        if i == 14:
            live.bridge.last_hb_t -= 10  # starve the watchdog
        live.tick()
    tw = live.twin
    a = live.session["start_step"]
    want = range_metrics(A.P, tw.rec, a, tw.k)
    got = replay_session(A.P, live.snapshot, live.session, controllers=("live",))["live"]
    assert json.dumps(want, sort_keys=True) == json.dumps(got, sort_keys=True)
    assert any(s["src"].startswith("SAFE") for s in live.session["steps"])
    assert any(s["src"].startswith("PLAN") for s in live.session["steps"])
