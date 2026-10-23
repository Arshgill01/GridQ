import time

import numpy as np

from conftest import bench_days
from gridq.common.config import load_params
from gridq.control.mpc import MPC, Oracle
from gridq.plant.outages import grid_schedule
from gridq.plant.twin import Timeline, Twin


def _twin(p, ds, n=2, seed=1000):
    test, pre, nxt = bench_days(p, ds, n)
    return Twin(p, Timeline.from_days(p, test + [nxt], n_sim=len(test),
                                      grid_sched=grid_schedule(p, len(test), seed), pre_days=pre))


def test_lp_optimal_with_duals_and_fast(p, ds):
    tw = _twin(p, ds)
    m = MPC(p)
    times = []
    for k in range(0, 96 * 2, 7):
        tw.k = k
        tw._activate()
        data, _ = m.build(tw.observe(), tw)
        from gridq.control import lp
        t0 = time.perf_counter()
        res = lp.solve(p, data)
        times.append(time.perf_counter() - t0)
        assert res.ok, res.status
        assert np.isfinite(res.shadow_price)
    assert np.median(times) < 1.0


def test_scenario_lp_solves(p, ds):
    ps = load_params(overrides={"mpc": {"scenarios": {"enabled": True}}})
    tw = _twin(ps, ds)
    m = MPC(ps)
    tw.k = 60
    tw._activate()
    data, _ = m.build(tw.observe(), tw)
    assert data.branches
    from gridq.control import lp
    res = lp.solve(ps, data)
    assert res.ok
    assert len(res.branch_cost) == len(data.branches)


def test_oracle_le_gridq(p, ds):
    from conftest import run_controller
    from gridq.bench.metrics import day_metrics
    o, _, _ = run_controller(p, ds, "Oracle", n_days=2)
    g, _, _ = run_controller(p, ds, "GridQ", n_days=2)
    tot = lambda tw: sum(day_metrics(p, tw.rec, d)["total_cost_rs"] for d in range(2))
    assert tot(o) <= tot(g) + 1e-6
