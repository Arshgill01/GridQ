import numpy as np

from gridq.bench.metrics import captured_pct, day_metrics, jain
from gridq.plant.twin import REC_FIELDS


def test_jain():
    assert jain([0, 0, 0]) == 1.0
    assert jain([5, 5, 5]) == 1.0
    assert abs(jain([10, 0, 0]) - 1 / 3) < 1e-12
    assert abs(jain([1, 2, 3]) - 36 / 42) < 1e-12


def test_captured():
    assert captured_pct(100, 80, 60) == 50.0
    assert captured_pct(100, 60, 60) == 100.0


def test_day_metrics_hand(p):
    rec = {f: np.zeros(96) for f in REC_FIELDS}
    rec["tariff"][:] = 8.0
    rec["grid"][:4] = 10.0              # 10 kW for 1 h -> 10 kWh x 8 = 80
    rec["dg"][4:8] = 4.0                # 4 kWh diesel -> 1.2 L -> 108 Rs
    rec["grid_up"][:] = 1
    rec["dem_clinic"][:] = 4.0
    rec["srv_clinic"][:] = 4.0
    rec["srv_clinic"][10] = 3.0         # one step below 99 % -> uptime 95/96, 0.25 kWh unserved
    rec["ch"][20:24] = 5.0              # 5 kWh charged
    rec["pv"][:] = 1.0
    rec["curt"][:48] = 1.0
    m = day_metrics(p, rec, 0)
    assert abs(m["energy_bill_rs"] - 80) < 1e-9
    assert abs(m["diesel_l"] - 1.2) < 1e-9
    assert abs(m["cost_rs"] - (80 + 108)) < 1e-9
    assert abs(m["critical_uptime_pct"] - 100 * 95 / 96) < 1e-9
    assert abs(m["critical_unserved_kwh"] - 0.25) < 1e-9
    assert abs(m["battery_wear_rs"] - 5 * 2.3) < 1e-9
    assert abs(m["solar_self_use_pct"] - 50) < 1e-9
    assert abs(m["peak_grid_kw"] - 10) < 1e-9
    expected_total = 80 + 108 + 10 * 15 + 5 * 2.3 + 0.25 * 500
    assert abs(m["total_cost_rs"] - expected_total) < 1e-9
