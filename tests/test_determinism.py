import json

import pytest

from conftest import run_controller
from gridq.bench.metrics import day_metrics
from gridq.plant.events import Event


@pytest.mark.parametrize("name", ["B0", "B1", "GridQ", "Oracle"])
def test_same_seed_same_metrics(p, ds, name):
    evs = lambda: [Event.make(60, "cloud"), Event.make(75, "grid_outage", {"on": True}),
                   Event.make(83, "grid_outage", {"on": False}), Event.make(85, "ev_fleet")]
    a, _, _ = run_controller(p, ds, name, n_days=2, events=evs())
    b, _, _ = run_controller(p, ds, name, n_days=2, events=evs())
    for d in range(2):
        ja = json.dumps(day_metrics(p, a.rec, d), sort_keys=True)
        jb = json.dumps(day_metrics(p, b.rec, d), sort_keys=True)
        assert ja == jb
