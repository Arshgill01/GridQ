"""Fetch NASA POWER hourly GHI + clear-sky GHI (+ T2M) for the site, for the I-BLEND dates.

Run once on a laptop with internet:
    python tools/fetch_nasa_power.py                 # dates from data/demand_15min.csv
    python tools/fetch_nasa_power.py 20130401 20170630

Writes data/solar_hourly.csv: timestamp (local solar time),ghi,clr,t2m
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gridq.common.config import load_params  # noqa: E402

API = "https://power.larc.nasa.gov/api/temporal/hourly/point"


def fetch(lat, lon, start: str, end: str) -> pd.DataFrame:
    params = dict(parameters="ALLSKY_SFC_SW_DWN,CLRSKY_SFC_SW_DWN,T2M", community="RE", latitude=lat,
                  longitude=lon, start=start, end=end, format="CSV", **{"time-standard": "LST"})
    for attempt in range(4):
        r = requests.get(API, params=params, timeout=120)
        if r.ok:
            break
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()
    txt = r.text
    body = txt[txt.index("-END HEADER-") + len("-END HEADER-"):].lstrip() if "-END HEADER-" in txt else txt
    df = pd.read_csv(io.StringIO(body))
    df["timestamp"] = pd.to_datetime(dict(year=df.YEAR, month=df.MO, day=df.DY, hour=df.HR))
    df = df.rename(columns={"ALLSKY_SFC_SW_DWN": "ghi", "CLRSKY_SFC_SW_DWN": "clr", "T2M": "t2m"})
    df = df[["timestamp", "ghi", "clr", "t2m"]]
    # NASA POWER fill value is -999: keep as missing (never silently faked)
    df.loc[df.ghi < -900, "ghi"] = float("nan")
    df.loc[df.clr < -900, "clr"] = float("nan")
    df.loc[df.t2m < -900, "t2m"] = float("nan")
    return df


def main():
    p = load_params()
    if len(sys.argv) == 3:
        a, b = pd.Timestamp(sys.argv[1]), pd.Timestamp(sys.argv[2])
    else:
        dem = pd.read_csv(ROOT / p["data"]["demand_csv"], usecols=["timestamp"], parse_dates=["timestamp"])
        a, b = dem.timestamp.min().normalize(), dem.timestamp.max().normalize()
    a -= pd.Timedelta(days=8)  # history for the first day's lag features
    parts = []
    y = a
    while y <= b:
        e = min(b, pd.Timestamp(year=y.year, month=12, day=31))
        print(f"fetching {y.date()} .. {e.date()} for {p['site']['lat']},{p['site']['lon']}")
        parts.append(fetch(p["site"]["lat"], p["site"]["lon"], y.strftime("%Y%m%d"), e.strftime("%Y%m%d")))
        y = e + pd.Timedelta(days=1)
    df = pd.concat(parts).drop_duplicates("timestamp").sort_values("timestamp")
    out = ROOT / p["data"]["solar_csv"]
    df.to_csv(out, index=False)
    print(f"wrote {out}: {len(df)} rows, {df.ghi.isna().sum()} missing ghi; "
          f"{df.timestamp.min()} .. {df.timestamp.max()}")


if __name__ == "__main__":
    main()
