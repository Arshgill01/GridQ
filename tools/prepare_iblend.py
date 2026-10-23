"""Raw I-BLEND -> data/demand_15min.csv + data/demand_meta.json (spec §6.3).

Verified layout (Readme.txt + inspection, 2018 release on figshare):
  energy_dataset/<building>.csv : "timestamp","power","current","voltage","frequency","power_factor"
  timestamp = UNIX seconds (convert in Asia/Kolkata), power in W, 1-minute samples, 2013-08-10 .. 2017-12-31
  dormitories have two meters each: <x>_hostel_mains.csv + <x>_hostel_ups.csv (backup feed); demand = sum
  iiitd_calender_schedule/calender_year_YYYY_.csv : Date, working_day, activity

Steps: kW -> 15-min means in IST -> fill gaps <= 30 min only -> drop days with < 95 % minute
coverage on any feeder (or any gap left) -> scale each feeder so its P95 over the TRAINING days
equals scale_to_p95_kw -> chronological split, last `test_days` good days held out.

    python tools/prepare_iblend.py
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gridq.common.config import load_params  # noqa: E402

FEEDERS = {
    "hostel_a": ["boys_hostel_mains", "boys_hostel_ups"],     # boys dormitory, mains + UPS feed
    "hostel_b": ["girls_hostel_mains", "girls_hostel_ups"],   # girls dormitory, mains + UPS feed
    "academic": ["acad_build_mains"],                          # academic building
}


def load_meter(raw: Path, name: str) -> pd.Series:
    f = raw / "energy_dataset" / f"{name}.csv"
    if not f.exists():
        with zipfile.ZipFile(raw / "energy_dataset.zip") as z:
            z.extract(f"energy_dataset/{name}.csv", raw)
    df = pd.read_csv(f, usecols=["timestamp", "power"])
    ts = pd.to_datetime(df.timestamp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    s = pd.Series(df.power.values / 1000.0, index=ts).sort_index()
    s = s[~s.index.duplicated()]
    s[(s < 0) | (s > 2000)] = np.nan          # meter glitches
    return s


def to_15min(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    s = s.resample("1min").mean()            # align to minute grid
    mean = s.resample("15min").mean()
    cnt = s.resample("15min").count()
    return mean, cnt


def main():
    p = load_params()
    raw = ROOT / p["data"]["iblend_dir"]
    meta = {"source": "I-BLEND (IIIT-Delhi), Rashid, Singh & Singh, Sci. Data 2019", "feeders": {}}
    series, coverage = {}, {}
    for feeder, meters in FEEDERS.items():
        parts = []
        covs = []
        for m in meters:
            s = load_meter(raw, m)
            mean, cnt = to_15min(s)
            parts.append(mean)
            covs.append(cnt)
            print(f"{m:22s} {s.index.min()} .. {s.index.max()}  {s.notna().sum():,} samples, "
                  f"median {s.median():.1f} kW")
        idx = parts[0].index
        for q in parts[1:]:
            idx = idx.union(q.index)
        tot = sum(q.reindex(idx) for q in parts)                 # NaN unless every meter present
        cov = sum(c.reindex(idx).fillna(0) for c in covs) / (15 * len(meters))
        series[feeder] = tot
        coverage[feeder] = cov
        meta["feeders"][feeder] = {"meters": meters}
    idx = series["hostel_a"].index.union(series["hostel_b"].index).union(series["academic"].index)
    full = pd.date_range(idx.min().normalize(), idx.max().normalize() + pd.Timedelta(hours=23, minutes=45), freq="15min")
    df = pd.DataFrame({f: series[f].reindex(full) for f in FEEDERS})
    cov = pd.DataFrame({f: coverage[f].reindex(full).fillna(0) for f in FEEDERS})
    # fill short gaps only (<= 2 slots = 30 min); longer gaps stay NaN -> the day is dropped
    df = df.interpolate(limit=2, limit_area="inside")
    day = df.index.normalize()
    day_cov = cov.groupby(day).mean().min(axis=1)
    day_nan = df.isna().groupby(day).sum().sum(axis=1)
    good = day_cov[(day_cov >= p["data"]["min_day_coverage"]) & (day_nan == 0)].index
    print(f"days: {len(day_cov)} total, {len(good)} good (coverage >= {p['data']['min_day_coverage']}, no gaps > 30 min)")
    df = df[day.isin(good)]
    days = sorted(set(df.index.normalize()))
    n_test = p["data"]["test_days"]
    test_days, train_days = days[-n_test:], days[:-n_test]
    split = np.where(df.index.normalize().isin(test_days), "test", "train")
    # scale on TRAINING days only (no information from the benchmark days)
    scale = {}
    for f in FEEDERS:
        p95 = float(np.percentile(df.loc[split == "train", f], 95))
        scale[f] = p["data"]["scale_to_p95_kw"][f] / p95
        df[f] = df[f] * scale[f]
        meta["feeders"][f].update(raw_p95_kw=round(p95, 2), scale=round(scale[f], 5))
    # published campus calendar (known in advance)
    cal = []
    for f in sorted((raw / "calendar" / "iiitd_calender_schedule").glob("*.csv")):
        cal.append(pd.read_csv(f))
    cal = pd.concat(cal)
    cal["Date"] = pd.to_datetime(cal["Date"])
    cal = cal.set_index("Date")
    dd = df.index.normalize()
    df["workday"] = cal["working_day"].reindex(dd).fillna(pd.Series(dd.weekday < 5, index=dd).astype(int)).astype(int).values
    df["activity"] = cal["activity"].reindex(dd).fillna("?").values
    df["split"] = split
    out = df.round(4)
    out.index.name = "timestamp"
    out.to_csv(ROOT / p["data"]["demand_csv"])
    meta.update(
        resolution="15 min means of 1-min samples, IST", units="kW (scaled; real shape and day-to-day variation kept)",
        n_good_days=len(days), train_range=[str(train_days[0].date()), str(train_days[-1].date())],
        test_range=[str(test_days[0].date()), str(test_days[-1].date())], n_train_days=len(train_days),
        n_test_days=len(test_days), min_day_coverage=p["data"]["min_day_coverage"],
        solar_site=f"NASA POWER @ {p['site']['lat']}, {p['site']['lon']} (IIIT-Delhi)",
        clinic="modelled (no public data): 3.5 kW + 1 kW 08-20 + fridge cycling, x N(1, 0.03)")
    (ROOT / p["data"]["meta_json"]).write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
