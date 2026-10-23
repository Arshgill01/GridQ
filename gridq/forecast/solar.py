"""PV forecast: clear-sky x EWMA clearness (spec §7.6). Stateless and causal:
recomputed from measured history every step, so it is deterministic and replayable."""
from __future__ import annotations

import numpy as np

from gridq.common.config import SPD

ALPHA = 0.5


def clearness(view, k: int) -> float:
    """EWMA (alpha 0.5) of measured/clear over the last 4 steps today with clear > 1 kW;
    initialised to the previous day's mean clearness."""
    day0 = k - (k % SPD)
    lo = day0 - SPD
    meas_prev = view.measured("pv", lo, day0)
    clr_prev = view.clear(lo, day0)
    ok = ~np.isnan(meas_prev)
    k_est = float(np.nansum(meas_prev[ok]) / max(np.sum(clr_prev[ok]), 1e-6)) if ok.any() and np.sum(clr_prev[ok]) > 1 else 0.8
    k_est = min(max(k_est, 0.05), 1.2)
    if k > day0:
        meas = view.measured("pv", day0, k)
        clr = view.clear(day0, k)
        idx = np.where(clr > 1.0)[0][-4:]
        for i in idx:
            k_est = ALPHA * min(meas[i] / clr[i], 1.2) + (1 - ALPHA) * k_est
    return k_est


def forecast_pv(view, k: int, T: int, pv_now: float) -> np.ndarray:
    ke = clearness(view, k)
    fc = view.clear(k, k + T) * ke
    fc = np.asarray(fc, float).copy()
    fc[0] = pv_now
    return fc
