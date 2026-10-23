import numpy as np
import pytest

from conftest import run_controller
from gridq.common.config import INFLEX
from gridq.control.safe import Safe
from gridq.plant.events import Event


@pytest.mark.parametrize("name", ["B0", "B1", "GridQ", "Oracle"])
def test_energy_balance_and_bounds(p, ds, name):
    tw, _, results = run_controller(p, ds, name, n_days=3)
    assert tw.max_residual < 1e-6
    for r in results:
        assert abs(r["residual"]) < 1e-6
    soc = tw.rec["soc"]
    assert soc.min() >= p["battery"]["soc_min_frac"] * p["battery"]["capacity_kwh"] - 1e-6
    assert soc.max() <= p["battery"]["soc_max_frac"] * p["battery"]["capacity_kwh"] + 1e-6
    assert tw.rec["tank"].min() >= -1e-9
    assert np.all(tw.rec["grid"] <= p["grid"]["import_limit_kw"] + 1e-6)
    # no import while the grid is down
    assert np.all(tw.rec["grid"][tw.rec["grid_up"] < 0.5] <= 1e-9)
    for l in INFLEX:
        assert np.all(tw.rec[f"srv_{l}"] <= tw.rec[f"dem_{l}"] + 1e-9)


def test_mask_gates_loads(p, ds):
    from conftest import bench_days, Timeline, Twin
    test, pre, nxt = bench_days(p, ds, 1)
    tw = Twin(p, Timeline.from_days(p, test + [nxt], n_sim=1, pre_days=pre))
    safe = Safe(p)
    for _ in range(96):
        tw.step(safe.decide(tw.observe()), applied_mask=0b11)
    for l in ("hostel_a", "hostel_b", "academic"):
        assert tw.rec[f"srv_{l}"].sum() == 0
    assert tw.rec["p_ev"].sum() == 0 and tw.rec["p_heater"].sum() == 0
    assert (tw.rec["srv_clinic"] > 0).all()


def test_events_apply(p, ds):
    evs = [Event.make(70, "grid_outage", {"on": True}), Event.make(80, "grid_outage", {"on": False}),
           Event.make(40, "cloud"), Event.make(50, "battery_fault"), Event.make(81, "ev_fleet")]
    tw, _, _ = run_controller(p, ds, "GridQ", n_days=1, events=evs)
    assert (tw.rec["grid"][70:80] == 0).all()
    assert tw.cap_f == 0.5
    assert tw.rec["soc"][50:].max() <= 0.5 * p["battery"]["capacity_kwh"] * p["battery"]["soc_max_frac"] + 1e-6
    assert any(e["type"] == "ev_fleet" for e in tw.event_log)
