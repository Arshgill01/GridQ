"""Explanations (spec §11), all computed from the LP: shadow price, counterfactual
re-solves in rupees, binding constraints, outage-hedge value and clinic survival time."""
from __future__ import annotations

import copy

import numpy as np

from gridq.common.config import DT, FLEX, INFLEX, SPD
from gridq.control import lp

NAMES = {"clinic": "Clinic", "pump": "Water pump", "hostel_a": "Hostel A", "hostel_b": "Hostel B",
         "academic": "Academic block", "ev": "EV charging", "heater": "Water heater"}
SHORT = {"clinic": "Clinic", "pump": "Pump", "hostel_a": "Hostel A", "hostel_b": "Hostel B",
         "academic": "Academic", "ev": "EV", "heater": "Heater"}
BINDING_TEXT = {
    "soc_min": "Battery at its minimum charge",
    "soc_max": "Battery full",
    "grid_cap": "Grid import at the {cap:.0f} kW limit",
    "islanded": "Islanded: no grid supply",
    "reserve_gap": "Battery below the clinic reserve (paying the reserve penalty)",
    "reserve_floor": "Battery held at the clinic reserve floor",
}


def hhmm(step_abs: int) -> str:
    sod = step_abs % SPD
    return f"{sod // 4:02d}:{(sod % 4) * 15:02d}"


def finish_step(data: lp.LPData, res: lp.LPResult, sess: dict) -> int | None:
    """First relative step by which the plan has delivered the session's remaining energy."""
    p = res.trunk[f"p_{sess['load']}"]
    cum = np.cumsum(p) * DT
    hit = np.where(cum >= sess["R"] - 1e-3)[0]
    return int(hit[0]) + 1 if len(hit) else None


def counterfactuals(p: dict, data: lp.LPData, res: lp.LPResult, k: int, max_items: int = 3) -> list[dict]:
    cands = []
    tr = res.trunk
    for f in FLEX:
        sess = [s for s in data.sessions if s["load"] == f and s["a"] <= 0 < s["d"] and s["R"] > 1e-3]
        if not sess or tr[f"p_{f}"][0] > 0.05 * p["loads"][f]["p_kw"]:
            continue
        s = min(sess, key=lambda s: s["d"])
        kw = min(p["loads"][f]["p_kw"], sum(x["R"] for x in sess) / DT)
        cands.append(("defer", f, kw * DT, dict(force_p0={f: kw}), s))
    for l in INFLEX:
        sh = tr[f"shed_{l}"][0]
        if sh > 1e-3:
            cands.append(("shed", l, sh * DT, dict(noshed0=[l]), None))
    cands.sort(key=lambda c: -c[2])
    out = []
    for kind, name, size, force, s in cands[:max_items]:
        d2 = copy.copy(data)
        d2.force_p0 = force.get("force_p0", {})
        d2.noshed0 = force.get("noshed0", [])
        r2 = lp.solve(p, d2)
        delta = (r2.objective - res.objective) if r2.ok else None
        item = {"kind": kind, "load": name, "delta_rs": delta, "kwh": size}
        if kind == "defer":
            fin = finish_step(data, res, s)
            dl = k + s["d"]
            met = fin is not None and fin <= s["d"]
            item.update(deadline=hhmm(dl), finishes=hhmm(k + fin) if fin else None, deadline_met=met)
            dtxt = f"Deadline {hhmm(dl)} still met (finishes {hhmm(k + fin)})." if met else \
                f"Deadline {hhmm(dl)} at risk."
            if delta is None:
                cost = "is not feasible"
            elif delta < 0.5:      # LP indifferent: several slots are equally cheap
                cost = "would cost the same (Rs 0): later slots are just as cheap, so it waits"
                delta = 0.0
            else:
                cost = f"costs Rs {delta:,.0f} more"
            item["text"] = f"{NAMES[name]} paused. Running it now {cost}. {dtxt}"
            item["lcd"] = f"{SHORT[name]} paused +Rs{delta:.0f}" if delta else f"{SHORT[name]} paused"
        else:
            pct = 100 * tr[f"shed_{name}"][0] / max(data.dem[name][0], 1e-6)
            cost = (f"would cost Rs {max(delta, 0):,.0f} more" if delta is not None else "is not physically possible right now")
            item["text"] = f"{NAMES[name]} trimmed {pct:.0f}%. Keeping it fully on {cost}."
            item["lcd"] = f"{SHORT[name]} -{pct:.0f}%"
        item["lcd"] = item["lcd"][:16]
        out.append(item)
    return out


def battery_idle(p: dict, data: lp.LPData, res: lp.LPResult) -> dict | None:
    """Battery holding charge while the grid is up: what would discharging 10 kW now cost?"""
    tr = res.trunk
    if data.grid_cap[0] <= 0 or tr["dis"][0] > 0.5 or tr["ch"][0] > 0.5:
        return None
    room = (data.soc0 - data.soc_min) * p["battery"]["eta_discharge"] / DT
    kw = min(10.0, data.p_dis, room)
    if kw < 1.0:
        return None
    d2 = copy.copy(data)
    d2.force_dis0 = kw
    r2 = lp.solve(p, d2)
    if not r2.ok:
        return None
    delta = r2.objective - res.objective
    per_kwh = delta / (kw * DT)
    return {"kind": "battery", "delta_rs": delta, "text":
            f"Battery holding {data.soc0:.0f} kWh. Discharging {kw:.0f} kW now would cost Rs {delta:,.0f} more over the plan "
            f"(Rs {per_kwh:.1f} per kWh delivered: wear plus buying it back later, against Rs {data.tariff[0]:.1f} grid now).",
            "lcd": f"Batt held +Rs{delta:.0f}"[:16]}


def hedge_text(data: lp.LPData, res: lp.LPResult) -> dict | None:
    """What the outage fan tree is buying: probability of an outage soon and its expected cost."""
    if not data.branches:
        return None
    prob = sum(b.weight for b in data.branches if b.tau <= 16)
    exp_cost = sum(b.weight * c for b, c in zip(data.branches, res.branch_cost))
    return {"kind": "hedge", "prob_4h": prob, "text":
            f"Outage risk in the next 4 h: {100 * prob:.0f}%. The plan keeps {res.trunk['soc'][0]:.0f} kWh in the "
            f"battery, hedging against it.", "expected_outage_rs": exp_cost}


def explain(p: dict, data: lp.LPData, res: lp.LPResult, k: int, budget_ok: bool = True) -> list[dict]:
    items = [{"kind": "price", "value": res.shadow_price,
              "text": f"Energy is worth Rs {res.shadow_price:.1f}/kWh right now.",
              "lcd": f"Rs{res.shadow_price:.1f}/kWh now"[:16]}]
    if budget_ok:
        items += counterfactuals(p, data, res, k)
        b = battery_idle(p, data, res)
        if b and b["delta_rs"] > 1:
            items.append(b)
    h = hedge_text(data, res)
    if h:
        items.append(h)
    for b in res.binding:
        items.append({"kind": "binding", "key": b,
                      "text": BINDING_TEXT[b].format(cap=p["grid"]["import_limit_kw"])})
    return items


def survival_hours(p: dict, soc: float, soc_min: float, p_dis: float, tank_l: float, clinic_p90: np.ndarray,
                   pv_fc: np.ndarray, cap_h: float = 48.0, use_dg: bool = True) -> float:
    """Grid down from now: serve only the clinic at P90 from forecast PV, then battery to soc_min,
    then diesel until the tank is empty. Returns hours until the first unmet step (capped)."""
    eta = p["battery"]["eta_discharge"]
    dmax = p["diesel"]["p_max_kw"]
    lpk = p["diesel"]["litres_per_kwh"]
    n = int(cap_h / DT)
    for t in range(n):
        need = max(0.0, clinic_p90[t % len(clinic_p90)] - (pv_fc[t] if t < len(pv_fc) else 0.0))
        dis = min(need, p_dis, max(0.0, (soc - soc_min) * eta / DT))
        soc -= dis / eta * DT
        need -= dis
        if need > 1e-9 and use_dg:
            dg = min(need, dmax, tank_l / (DT * lpk))
            tank_l -= dg * DT * lpk
            need -= dg
        if need > 1e-6:
            return t * DT
    return cap_h
