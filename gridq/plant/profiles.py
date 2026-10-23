"""Demand and solar profiles for the twin.

Real data (I-BLEND demand, NASA POWER solar) is loaded from the processed CSVs.
If a file is missing, a seeded synthetic fallback is used and `fallback` flags
are set so the dashboard and results.json can say so (honesty rule).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from gridq.common.config import DT, IBLEND_FEEDERS, ROOT, SPD

log = logging.getLogger("gridq.profiles")


@dataclass
class DayData:
    date: str                      # YYYY-MM-DD
    weekday: int
    demand: dict                   # feeder -> np.ndarray(96) kW (true)
    pv: np.ndarray                 # kW (true, before events)
    pv_clear: np.ndarray           # kW clear-sky
    split: str = "test"            # train | test
    workday: bool = True           # campus working day (published IIIT-D calendar; weekday rule if absent)


@dataclass
class Dataset:
    days: list                     # list[DayData] in chronological order
    fallback: dict = field(default_factory=dict)   # {"demand": bool, "solar": bool}
    meta: dict = field(default_factory=dict)

    def test_days(self):
        return [d for d in self.days if d.split == "test"]

    def train_days(self):
        return [d for d in self.days if d.split == "train"]


# ---------------------------------------------------------------- clinic
def clinic_profile(p: dict, rng: np.random.Generator) -> np.ndarray:
    c = p["clinic_model"]
    hours = np.arange(SPD) * DT
    base = np.full(SPD, c["base_kw"])
    base += np.where((hours >= c["day_hours"][0]) & (hours < c["day_hours"][1]), c["day_extra_kw"], 0.0)
    half = c["fridge_period_min"] / 15 / 2  # steps per half period
    sq = np.where((np.arange(SPD) // max(half, 1)) % 2 == 0, 1.0, -1.0) * c["fridge_kw"]
    return np.maximum(0.0, (base + sq) * rng.normal(1.0, c["noise_sd"], SPD))


def clinic_nominal(p: dict) -> np.ndarray:
    """Noise-free clinic profile: what the forecaster knows."""
    c = p["clinic_model"]
    hours = np.arange(SPD) * DT
    base = np.full(SPD, c["base_kw"])
    base += np.where((hours >= c["day_hours"][0]) & (hours < c["day_hours"][1]), c["day_extra_kw"], 0.0)
    half = c["fridge_period_min"] / 15 / 2
    sq = np.where((np.arange(SPD) // max(half, 1)) % 2 == 0, 1.0, -1.0) * c["fridge_kw"]
    return base + sq


# ---------------------------------------------------------------- synthetic fallback
def _synthetic_demand(rng: np.random.Generator) -> dict:
    h = np.arange(SPD) * DT
    ha = 8 + 6 * ((h >= 6) & (h < 9)) + 12 * (h >= 18)
    hb = np.roll(ha, 2) * 0.9
    ac = np.where((h >= 9) & (h < 17), 25.0, 3.0)
    return {
        "hostel_a": ha * rng.normal(1, 0.05, SPD),
        "hostel_b": hb * rng.normal(1, 0.05, SPD),
        "academic": ac * rng.normal(1, 0.05, SPD),
    }


def _synthetic_solar(p: dict, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    h = np.arange(SPD) * DT
    ghi_clr = np.where((h > 6) & (h < 18), 1000 * np.sin(np.pi * (h - 6) / 12), 0.0)
    k = rng.uniform(0.5, 1.0)
    kw = p["solar"]["kwp"] * p["solar"]["performance_ratio"] / 1000
    return ghi_clr * k * kw, ghi_clr * kw


# ---------------------------------------------------------------- loading
def load_solar(p: dict) -> pd.DataFrame | None:
    path = ROOT / p["data"]["solar_csv"]
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.set_index("timestamp").sort_index()
    df[["ghi", "clr"]] = df[["ghi", "clr"]].clip(lower=0)
    # NASA POWER hourly value HR=h is the mean over [h, h+1) local solar time: centre it at h:30,
    # then linearly interpolate to 15-min step starts (gaps longer than 2 h stay missing)
    df.index = df.index + pd.Timedelta(minutes=30)
    full = pd.date_range(df.index.min().floor("D"), df.index.max().ceil("D"), freq="15min")
    q = df[["ghi", "clr"]].reindex(df.index.union(full)).interpolate("time", limit=8, limit_area="inside")
    q = q.reindex(full)
    night = q.index.hour.isin([0, 1, 2, 3, 4, 21, 22, 23])
    q.loc[night] = q.loc[night].fillna(0.0)
    return q


def split_column(p: dict, fold: str | None = None) -> str:
    fold = fold or p["bench"].get("fold", "winter")
    spec = p["data"].get("folds", {}).get(fold, {"last": True})
    return "split" if spec.get("last") else f"split_{fold}"


def load_dataset(p: dict, synthetic_days: int = 60, seed: int = 7, fold: str | None = None) -> Dataset:
    """Load processed real data; fall back to synthetic, flagged. `fold` picks the held-out block."""
    demand_path = ROOT / p["data"]["demand_csv"]
    kw = p["solar"]["kwp"] * p["solar"]["performance_ratio"] / 1000
    solar = load_solar(p)
    days: list[DayData] = []
    fallback = {"demand": False, "solar": solar is None}
    meta: dict = {}

    if demand_path.exists():
        dem = pd.read_csv(demand_path, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
        meta_path = ROOT / p["data"]["meta_json"]
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
        scol = split_column(p, fold)
        sol_by_date = {str(d): g for d, g in solar.groupby(solar.index.date)} if solar is not None else {}
        for date, g in dem.groupby(dem.index.date):
            if len(g) != SPD:
                continue
            dstr = str(date)
            rng = np.random.default_rng(_date_seed(dstr))
            s = sol_by_date.get(dstr)
            if s is not None and len(s) >= SPD and not s["ghi"].iloc[:SPD].isna().any():
                pv = s["ghi"].values[:SPD] * kw
                pvc = s["clr"].values[:SPD] * kw
                solar_ok = True
            else:
                pv, pvc = _synthetic_solar(p, rng)
                solar_ok = False
            if not solar_ok:
                fallback["solar"] = True
            days.append(DayData(
                date=dstr, weekday=pd.Timestamp(dstr).weekday(),
                demand={f: g[f].values.astype(float) for f in IBLEND_FEEDERS},
                pv=np.asarray(pv, float), pv_clear=np.asarray(pvc, float),
                split=str(g[scol].iloc[0]) if scol in g else "test",
                workday=bool(g["workday"].iloc[0]) if "workday" in g else pd.Timestamp(dstr).weekday() < 5,
            ))
    else:
        log.warning("I-BLEND demand CSV missing: using SYNTHETIC demand (flagged)")
        fallback["demand"] = True
        rng = np.random.default_rng(seed)
        start = pd.Timestamp("2017-01-01")
        n_test = p["data"]["test_days"]
        for i in range(synthetic_days):
            d = start + pd.Timedelta(days=i)
            pv, pvc = _synthetic_solar(p, rng)
            days.append(DayData(
                date=str(d.date()), weekday=d.weekday(), demand=_synthetic_demand(rng),
                pv=pv, pv_clear=pvc, split="test" if i >= synthetic_days - n_test else "train",
                workday=d.weekday() < 5))
        meta = {"source": "SYNTHETIC fallback (I-BLEND unavailable)"}
    if fallback["solar"]:
        log.warning("NASA POWER solar missing for some days: synthetic clear-sky bell used (flagged)")
    return Dataset(days=days, fallback=fallback, meta=meta)


def _date_seed(dstr: str) -> int:
    return int(dstr.replace("-", "")) % (2**31)
