"""Per-day and aggregate metrics (spec §12, plus the total cost of service, D1)."""
from __future__ import annotations

import numpy as np

from gridq.common.config import DT, FAIR, FLEX, INFLEX, SPD


def jain(x) -> float:
    x = np.asarray(x, float)
    if np.all(x <= 1e-12):
        return 1.0
    return float(x.sum() ** 2 / (len(x) * np.sum(x ** 2)))


def day_metrics(p: dict, rec: dict, day: int) -> dict:
    return range_metrics(p, rec, day * SPD, (day + 1) * SPD)


def range_metrics(p: dict, rec: dict, a: int, b: int) -> dict:
    r = {k: v[a:b] for k, v in rec.items()}
    d = p["diesel"]
    wear = p["battery"]["wear_rs_per_kwh"]
    L = p["loads"]
    energy_bill = float(np.sum(r["tariff"] * r["grid"]) * DT)
    diesel_l = float(np.sum(r["dg"]) * DT * d["litres_per_kwh"])
    diesel_rs = diesel_l * d["rs_per_litre"]
    cost = energy_bill + diesel_rs
    peak = float(np.max(r["grid"]))
    peak_rs = peak * p["grid"]["peak_charge_rs_per_kw"]
    wear_rs = float(np.sum(r["ch"] + r["dis"]) * DT * wear)
    unserved_l = {l: float(np.sum(r[f"dem_{l}"] - r[f"srv_{l}"]) * DT) for l in INFLEX}
    voll_rs = sum(unserved_l[l] * L[l]["voll"] for l in INFLEX)
    flex_missed = float(np.sum(r["flex_missed"]))
    flex_rs = sum(float(np.sum(r[f"miss_{f}"])) * L[f]["slack_rs"] for f in FLEX)
    clinic_ok = r["srv_clinic"] >= 0.99 * r["dem_clinic"] - 1e-9
    shed_min = {l: float(np.sum(15.0 * np.where(r[f"dem_{l}"] > 1e-9,
                                               (r[f"dem_{l}"] - r[f"srv_{l}"]) / np.maximum(r[f"dem_{l}"], 1e-9), 0)))
                for l in INFLEX}
    pv_tot = float(np.sum(r["pv"]))
    return {
        "cost_rs": cost,
        "energy_bill_rs": energy_bill,
        "diesel_rs": diesel_rs,
        "peak_charge_rs": peak_rs,
        "battery_wear_rs": wear_rs,
        "unserved_value_rs": voll_rs + flex_rs,
        "total_cost_rs": cost + peak_rs + wear_rs + voll_rs + flex_rs,
        "critical_uptime_pct": float(100 * np.mean(clinic_ok)),
        "critical_unserved_kwh": unserved_l["clinic"],
        "unserved_kwh_total": float(sum(unserved_l.values()) + flex_missed),
        "deadline_misses": int(np.sum(r["misses"])),
        "diesel_l": diesel_l,
        "co2_kg": diesel_l * d["co2_kg_per_litre"] + float(np.sum(r["grid"]) * DT) * p["grid"]["emission_kg_per_kwh"],
        "peak_grid_kw": peak,
        "solar_self_use_pct": float(100 * (pv_tot - np.sum(r["curt"])) / pv_tot) if pv_tot > 1e-9 else 100.0,
        "shed_minutes": shed_min,
        "fairness_jain": jain([shed_min[l] for l in FAIR]),
        "outage_steps": int(np.sum(r["grid_up"] < 0.5)),
    }


def settle(p: dict, soc_start: float, soc_end: float) -> float:
    """End-of-run battery settlement (Rs): energy left below the start is a cost, above it a credit."""
    return (soc_start - soc_end) * p["bench"]["settle_rs_per_kwh"]


def captured_pct(b1: float, gq: float, oracle: float) -> float:
    gap = b1 - oracle
    return float(100 * (b1 - gq) / gap) if abs(gap) > 1e-9 else float("nan")


SCALARS = ["cost_rs", "total_cost_rs", "energy_bill_rs", "diesel_rs", "peak_charge_rs", "battery_wear_rs",
           "unserved_value_rs", "critical_uptime_pct", "critical_unserved_kwh", "unserved_kwh_total",
           "deadline_misses", "diesel_l", "co2_kg", "peak_grid_kw", "solar_self_use_pct", "fairness_jain"]


def summarise(rows: list[dict]) -> dict:
    out = {}
    for k in SCALARS:
        v = np.array([r[k] for r in rows], float)
        out[k] = {"median": float(np.median(v)), "p10": float(np.percentile(v, 10)), "p90": float(np.percentile(v, 90)),
                  "min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()), "sum": float(v.sum())}
    return out
