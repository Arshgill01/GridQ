"""Runtime quantile forecaster: loads data/models/demand_models.joblib (forecast/train.py)."""
from __future__ import annotations

import joblib
import numpy as np

from gridq.common.config import IBLEND_FEEDERS, SPD
from gridq.forecast import features as F
from gridq.forecast.demand import NaiveForecaster

HIST = 8 * SPD


class QuantileForecaster(NaiveForecaster):
    name = "quantile-GBM"
    fallback = False

    def __init__(self, p, mdir):
        super().__init__(p)
        self.models = joblib.load(mdir / "demand_models.joblib")

    def feeders(self, view, k: int, T: int, now: dict):
        H = min(T, SPD) - 1                      # horizons 1..95 (direct model); t=0 is the measurement
        hs = np.arange(1, H + 1)
        sod = (np.arange(k - HIST, k + SPD)) % SPD
        wd, work = view.calendar(k - HIST, k + SPD)
        origin = np.full(H, HIST)
        p50, p90 = {}, {}
        for f in IBLEND_FEEDERS:
            y = np.concatenate([view.measured(f, k - HIST, k), np.full(SPD, np.nan)])
            X = F.build(y, origin, hs, sod, wd, work)
            m = self.models[f]
            a = np.maximum(m["p50"].predict(X), 0.0)
            b = m["p90"].predict(X) + m["off90"][np.minimum((hs - 1) // 24, 3)]
            b = np.maximum(b, a)
            fc50 = np.empty(T)
            fc90 = np.empty(T)
            fc50[0] = fc90[0] = now[f]
            fc50[1:H + 1], fc90[1:H + 1] = a, b
            if T > H + 1:   # beyond the direct horizon: repeat the last day's shape
                fc50[H + 1:] = fc50[1:T - H]
                fc90[H + 1:] = fc90[1:T - H]
            p50[f], p90[f] = fc50, fc90
        return p50, p90
