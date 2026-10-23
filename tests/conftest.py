import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gridq.bench.run_days import bench_days, make_controller  # noqa: E402
from gridq.common.config import load_params  # noqa: E402
from gridq.plant.outages import grid_schedule  # noqa: E402
from gridq.plant.profiles import load_dataset  # noqa: E402
from gridq.plant.twin import Timeline, Twin  # noqa: E402


@pytest.fixture(scope="session")
def p():
    return load_params()


@pytest.fixture(scope="session")
def ds(p):
    return load_dataset(p)


def run_controller(p, ds, name, n_days=3, seed=1000, events=None):
    test, pre, nxt = bench_days(p, ds, n_days)
    tl = Timeline.from_days(p, test + [nxt], n_sim=len(test), grid_sched=grid_schedule(p, len(test), seed),
                            pre_days=pre)
    tw = Twin(p, tl)
    c = make_controller(name, p)
    events = sorted(events or [], key=lambda e: e.step)
    results = []
    for k in range(tl.n_steps):
        while events and events[0].step == k:
            tw.apply(events.pop(0))
        obs = tw.observe()
        results.append(tw.step(c.decide(obs, tw)))
    return tw, c, results
