import json

import numpy as np
import pandas as pd
import pytest

from gridq.common.config import ROOT, SPD
from gridq.forecast import features as F

DEM = ROOT / "data" / "demand_15min.csv"
METRICS = ROOT / "data" / "models" / "metrics.json"


@pytest.mark.skipif(not DEM.exists(), reason="no processed I-BLEND data")
def test_no_benchmark_date_in_training():
    df = pd.read_csv(DEM, usecols=["timestamp", "split"], parse_dates=["timestamp"])
    d = df.groupby(df.timestamp.dt.date)["split"].agg(set)
    assert all(len(s) == 1 for s in d)                     # a day is train or test, never both
    train = {k for k, s in d.items() if s == {"train"}}
    test = {k for k, s in d.items() if s == {"test"}}
    assert train and test and not (train & test)
    assert max(train) < min(test)                           # chronological split


@pytest.mark.skipif(not METRICS.exists(), reason="no trained models")
def test_model_quality_or_flagged():
    m = json.loads(METRICS.read_text())
    h = m["held_out"]
    if h["p50_mae_kw"] >= h["naive_mae_kw"]:
        assert m["beats_naive"] is False                   # then GridQ must fall back, flagged
    else:
        assert m["beats_naive"] is True
    assert 80.0 <= h["p90_coverage_pct"] <= 97.0
    lo, hi = m["train_range"]
    t0, _ = m["test_range"]
    assert hi < t0


def test_features_use_no_future():
    """Perturbing anything at or after the origin must not change the features."""
    rng = np.random.default_rng(0)
    n = 10 * SPD
    y = rng.normal(10, 2, n)
    sod = np.arange(n) % SPD
    wd = (np.arange(n) // SPD) % 7
    work = (wd < 5).astype(float)
    k0 = 8 * SPD + 7
    origins = np.full(SPD - 1, k0)
    hs = np.arange(1, SPD)
    X1 = F.build(y, origins, hs, sod, wd, work)
    y2 = y.copy()
    y2[k0:] = rng.normal(50, 9, n - k0)                     # rewrite the future
    X2 = F.build(y2, origins, hs, sod, wd, work)
    np.testing.assert_array_equal(np.nan_to_num(X1, nan=-1), np.nan_to_num(X2, nan=-1))


def test_inference_reads_only_past(p, ds):
    from conftest import run_controller
    from gridq.forecast.demand import load_forecaster
    tw, _, _ = run_controller(p, ds, "B1", n_days=1)
    fc = load_forecaster(p)
    tw.k = 60                                                # pretend we are at step 60
    with pytest.raises(AssertionError):
        tw.measured("hostel_a", 50, 61)                     # reading step 60+ must be impossible
    p50, p90 = fc.forecast(tw, 60, 96, tw.demand_actual(60))
    assert len(p50["hostel_a"]) == 96 and np.all(p90["hostel_a"] >= p50["hostel_a"] - 1e-9)
