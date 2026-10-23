"""Features for the direct multi-horizon quantile demand model (shared by train and inference).

One row = (forecast origin k, horizon h). Target time tau = k + h. Everything used is
known at the origin: calendar of the target time (incl. the published campus
calendar), values at tau-96 / tau-672 (always <= k-1 for h <= 95), and the level
of the series just before the origin. No future data (tests/test_forecast.py).
"""
from __future__ import annotations

import numpy as np

SPD = 96
FEATURES = ["h", "tod_sin", "tod_cos", "weekday", "weekend", "workday", "lag96", "lag672", "last1", "last4",
            "last96", "dev_yday", "lag96_ratio"]


def build(y: np.ndarray, origin_idx: np.ndarray, horizons: np.ndarray, sod0: np.ndarray, weekday: np.ndarray,
          workday: np.ndarray) -> np.ndarray:
    """y: full series (NaN where missing), indexed so that y[i] is step i.
    origin_idx: array of origins k (features may use y[:k] only).
    horizons: per-row horizon h (>=1, <=95).
    sod0 / weekday / workday: per-step calendar arrays aligned with y (known in advance).
    Returns X with columns FEATURES."""
    k = origin_idx
    h = horizons
    tau = k + h
    assert np.all(h >= 1) and np.all(h <= SPD - 1)
    sod = sod0[tau]
    ang = 2 * np.pi * sod / SPD
    cs = np.nancumsum(np.nan_to_num(y))
    cnt = np.cumsum(~np.isnan(y))

    def win_mean(end, n):   # mean of y[end-n:end]
        a = np.clip(end - n, 0, None)
        s = cs[end - 1] - np.where(a > 0, cs[a - 1], 0)
        c = cnt[end - 1] - np.where(a > 0, cnt[a - 1], 0)
        return np.where(c > 0, s / np.maximum(c, 1), np.nan)

    def at(i):
        out = np.full(len(i), np.nan)
        ok = i >= 0
        out[ok] = y[i[ok]]
        return out

    lag96 = at(tau - SPD)
    lag672 = at(tau - 7 * SPD)
    last1 = at(k - 1)
    last4 = win_mean(k, 4)
    last96 = win_mean(k, SPD)
    yday_at_origin = at(k - 1 - SPD)
    return np.column_stack([
        h, np.sin(ang), np.cos(ang), weekday[tau], (weekday[tau] >= 5).astype(float), workday[tau],
        lag96, lag672, last1, last4, last96, last1 - yday_at_origin,
        lag96 / np.where(np.abs(lag672) > 1e-6, lag672, np.nan),
    ])
