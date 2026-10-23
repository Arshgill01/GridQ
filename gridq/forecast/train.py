"""Train P50/P90 quantile GBMs per I-BLEND feeder (spec §7.6). Laptop, seconds.

    python -m gridq.forecast.train

Chronological protocol (DECISIONS.md D6):
  train days  = everything before the held-out benchmark days, minus the last CAL_DAYS
  calibration = the last CAL_DAYS training days: split-conformal offset so the P90 really covers ~90 %
  held-out    = the benchmark (test) days: reported, never used for fitting or calibration
Writes data/models/demand_models.joblib and data/models/metrics.json.
"""
from __future__ import annotations

import json
import time

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from gridq.common.config import IBLEND_FEEDERS, ROOT, load_params
from gridq.forecast import features as F

SPD = 96
CAL_DAYS = 45
RNG = np.random.default_rng(0)


def load_series(p):
    df = pd.read_csv(ROOT / p["data"]["demand_csv"], parse_dates=["timestamp"]).set_index("timestamp").sort_index()
    full = pd.date_range(df.index.min().normalize(), df.index.max().normalize() + pd.Timedelta(hours=23, minutes=45), freq="15min")
    d = df.reindex(full)
    split = d["split"].fillna("gap").values
    day = full.normalize()
    weekday = full.weekday.values.astype(float)
    workday = d["workday"].astype(float).values if "workday" in d else (weekday < 5).astype(float)
    # calendar is known in advance even on dropped days: fill from the published calendar / weekday rule
    if np.isnan(workday).any():
        wd = pd.Series(workday, index=full).groupby(day).transform("max").values
        workday = np.where(np.isnan(wd), (weekday < 5).astype(float), wd)
    sod = ((full.hour * 60 + full.minute) // 15).values
    return d, full, split, sod, weekday, workday


def rows(y, origins, sod, weekday, workday, all_h=False):
    if all_h:
        k = np.repeat(origins, SPD - 1)
        h = np.tile(np.arange(1, SPD), len(origins))
    else:
        k = np.repeat(origins, 24)
        h = RNG.integers(1, SPD, size=len(k))
    X = F.build(y, k, h, sod, weekday, workday)
    t = y[k + h]
    ok = ~np.isnan(t) & ~np.isnan(X[:, F.FEATURES.index("lag96")]) & ~np.isnan(X[:, F.BASE])
    return X[ok], t[ok]


def pinball(y, q, tau):
    d = y - q
    return float(np.mean(np.maximum(tau * d, (tau - 1) * d)))


def main():
    t0 = time.time()
    p = load_params()
    d, full, split, sod, weekday, workday = load_series(p)
    idx = np.arange(len(full))
    train_days = sorted(set(full[split == "train"].normalize()))
    test_days = sorted(set(full[split == "test"].normalize()))
    cal_days = set(train_days[-CAL_DAYS:])
    fit_days = set(train_days[:-CAL_DAYS])
    is_day = lambda days: np.asarray(full.normalize().isin(list(days)))
    m_fit, m_cal, m_test = is_day(fit_days), is_day(cal_days), is_day(test_days)
    # origins: 8 per day (every 3 h) for fitting; every 4 steps for calibration / test
    first_ok = 7 * SPD + 1
    o_fit = idx[m_fit & (idx % 12 == 0) & (idx >= first_ok) & (idx < len(full) - SPD)]
    o_cal = idx[m_cal & (idx % 4 == 0) & (idx < len(full) - SPD)]
    o_test = idx[m_test & (idx % 4 == 0) & (idx < len(full) - SPD)]
    # no target of a fitting/calibration row may land on a held-out day
    test_start = idx[m_test].min()
    o_fit = o_fit[o_fit + SPD - 1 < test_start]
    o_cal = o_cal[o_cal + SPD - 1 < test_start]

    models, report = {}, {"feeders": {}}
    all_y, all_p50, all_p90, all_naive = [], [], [], []
    for f in IBLEND_FEEDERS:
        y = d[f].values.astype(float)
        Xf, yf = rows(y, o_fit, sod, weekday, workday)
        kw = dict(max_iter=300, learning_rate=0.06, max_leaf_nodes=31, min_samples_leaf=80, l2_regularization=1.0,
                  random_state=0)
        rf = yf - Xf[:, F.BASE]                       # residual over the calendar-aware naive base
        m50 = HistGradientBoostingRegressor(loss="quantile", quantile=0.5, **kw).fit(Xf, rf)
        m90 = HistGradientBoostingRegressor(loss="quantile", quantile=0.9, **kw).fit(Xf, rf)
        # split-conformal calibration of the P90 (one offset per horizon block of 6 h)
        Xc, yc = rows(y, o_cal, sod, weekday, workday, all_h=True)
        q90c = m90.predict(Xc) + Xc[:, F.BASE]
        hb = np.minimum((Xc[:, 0] - 1) // 24, 3).astype(int)
        off = np.zeros(4)
        for b in range(4):
            e = yc[hb == b] - q90c[hb == b]
            n = len(e)
            off[b] = np.quantile(e, min(1.0, 0.9 * (n + 1) / n)) if n else 0.0
        # model selection on the CALIBRATION days (never the test days): GBM vs calendar-aware naive
        p50c = m50.predict(Xc) + Xc[:, F.BASE]
        use_model = bool(np.mean(np.abs(yc - p50c)) < np.mean(np.abs(yc - Xc[:, F.BASE])))
        Xt, yt = rows(y, o_test, sod, weekday, workday, all_h=True)
        p50 = m50.predict(Xt) + Xt[:, F.BASE] if use_model else Xt[:, F.BASE].copy()
        hbt = np.minimum((Xt[:, 0] - 1) // 24, 3).astype(int)
        p90_raw = m90.predict(Xt) + Xt[:, F.BASE]
        p90 = np.maximum(p90_raw + off[hbt], p50)
        naive = Xt[:, F.FEATURES.index("lag96")]
        same = Xt[:, F.FEATURES.index("lag_same")]
        r_same = float(np.nanmean(np.abs(yt - np.where(np.isnan(same), naive, same))))
        r = {"n_fit": int(len(yf)), "n_cal": int(len(yc)), "n_test": int(len(yt)),
             "p50_mae_kw": float(np.mean(np.abs(yt - p50))), "naive_mae_kw": float(np.mean(np.abs(yt - naive))),
             "p90_coverage_raw_pct": float(100 * np.mean(yt <= p90_raw)),
             "p90_coverage_pct": float(100 * np.mean(yt <= p90)), "p90_pinball": pinball(yt, p90, 0.9),
             "naive_p90_coverage_pct": float(100 * np.mean(yt <= naive * 1.2)),
             "same_daytype_mae_kw": r_same, "p50_source": "gbm" if use_model else "calendar-naive (chosen on calibration days)",
             "conformal_offsets_kw": off.round(3).tolist()}
        report["feeders"][f] = r
        models[f] = {"p50": m50 if use_model else None, "p90": m90, "off90": off}
        all_y.append(yt); all_p50.append(p50); all_p90.append(p90); all_naive.append(naive)
        print(f"{f:9s} P50 MAE {r['p50_mae_kw']:.3f} kW vs naive {r['naive_mae_kw']:.3f} | "
              f"P90 cover {r['p90_coverage_raw_pct']:.1f}% raw -> {r['p90_coverage_pct']:.1f}% calibrated | P50 {r['p50_source']}")
    Y, P50, P90, N = map(np.concatenate, (all_y, all_p50, all_p90, all_naive))
    held = {"p50_mae_kw": float(np.mean(np.abs(Y - P50))), "naive_mae_kw": float(np.mean(np.abs(Y - N))),
            "p90_coverage_pct": float(100 * np.mean(Y <= P90)), "p90_pinball": pinball(Y, P90, 0.9)}
    report["held_out"] = held
    report["beats_naive"] = bool(held["p50_mae_kw"] < held["naive_mae_kw"])
    report["model"] = (f"HistGradientBoostingRegressor quantile 0.5/0.9 per feeder, direct multi-horizon, "
                       f"{len(F.FEATURES)} causal features; P90 split-conformal on the last {CAL_DAYS} training days")
    report["train_range"] = [str(min(fit_days).date()), str(max(cal_days).date())]
    report["test_range"] = [str(test_days[0].date()), str(test_days[-1].date())]
    report["features"] = F.FEATURES
    report["train_s"] = time.time() - t0
    out = ROOT / "data" / "models"
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(models, out / "demand_models.joblib", compress=3)
    (out / "metrics.json").write_text(json.dumps(report, indent=1))
    print(f"held-out: P50 MAE {held['p50_mae_kw']:.3f} vs naive {held['naive_mae_kw']:.3f} kW, "
          f"P90 coverage {held['p90_coverage_pct']:.1f}%  beats_naive={report['beats_naive']}  ({report['train_s']:.0f}s)")


if __name__ == "__main__":
    main()
