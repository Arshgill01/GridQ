"""Demand forecasts: P50 and P90 per I-BLEND feeder + the modelled clinic.

`QuantileForecaster` loads the trained quantile GBMs (forecast/train.py). If the
models are missing, or failed to beat seasonal-naive on held-out days, it falls
back to seasonal naive and sets `fallback=True` (shown on the dashboard).
"""
from __future__ import annotations

import json
import logging

import numpy as np

from gridq.common.config import IBLEND_FEEDERS, ROOT, SPD
from gridq.plant.profiles import clinic_nominal

log = logging.getLogger("gridq.forecast")
NAIVE_P90_RATIO = 1.2


class NaiveForecaster:
    """Same time yesterday. P90 = P50 x 1.2 (spec's documented cut-down)."""
    name = "seasonal-naive"
    fallback = True

    def __init__(self, p):
        self.p = p
        self.clinic = clinic_nominal(p)

    def feeders(self, view, k: int, T: int, now: dict):
        p50, p90 = {}, {}
        for f in IBLEND_FEEDERS:
            y = view.measured(f, k - SPD, k)            # yesterday, steps k-96 .. k-1
            y = _fill(y)
            fc = np.array([y[t % SPD] for t in range(T)])  # target k+t -> k+t-96 (t<96) or k+t-192
            fc[0] = now[f]
            p50[f] = fc
            p90[f] = fc * NAIVE_P90_RATIO
            p90[f][0] = now[f]
        return p50, p90

    def clinic_fc(self, k: int, T: int, now: float):
        sod = (k + np.arange(T)) % SPD
        p50 = self.clinic[sod].copy()
        p50[0] = now
        return p50, p50 * 1.1

    def forecast(self, view, k: int, T: int, now: dict):
        p50, p90 = self.feeders(view, k, T, now)
        c50, c90 = self.clinic_fc(k, T, now["clinic"])
        p50["clinic"], p90["clinic"] = c50, c90
        return p50, p90


def _fill(y: np.ndarray) -> np.ndarray:
    if np.isnan(y).all():
        return np.zeros_like(y)
    m = np.nanmean(y)
    return np.where(np.isnan(y), m, y)


def load_forecaster(p: dict):
    """Quantile models if available and validated, else seasonal naive (flagged)."""
    try:
        from gridq.forecast.model import QuantileForecaster
        fold = p["bench"].get("fold", "winter")
        mdir = ROOT / "data" / "models" / ("" if fold == "winter" else fold)
        meta = json.loads((mdir / "metrics.json").read_text())
        if not meta.get("beats_naive", False):
            log.warning("quantile model did not beat seasonal naive on held-out days: using naive (flagged)")
            return NaiveForecaster(p)
        return QuantileForecaster(p, mdir)
    except (FileNotFoundError, ImportError):
        log.warning("no trained demand models: using seasonal naive (flagged)")
        return NaiveForecaster(p)
