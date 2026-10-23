"""Rolling-horizon LP shared by the GridQ MPC and the Oracle (spec §8).

Pure LP (no binaries) so it yields duals. Assembled directly as sparse matrices
and solved with HiGHS via scipy (DECISIONS.md D2: ~10x faster than CVXPY and one
less dependency on the board).

Optional outage fan tree (DECISIONS.md D3): a *trunk* (the forecast future) plus
*branches*. Branch s shares every decision with the trunk before its divergence
step tau_s, then follows its own grid availability (an outage) until end_s, after
which it rejoins the trunk; the difference in battery energy at the rejoin is
valued at the terminal value. Each step's cost is weighted by the probability
mass on that path, so the objective is an expected cost and the first-step
decision hedges against outages it cannot see coming.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog

from gridq.common.config import DT, FAIR, FLEX, INFLEX


@dataclass
class Branch:
    tau: int                 # first step owned by the branch (>= 1)
    end: int                 # rejoin step (exclusive), <= T
    cap: np.ndarray          # grid import cap per owned step (len end - tau)
    weight: float            # probability of this path


@dataclass
class LPData:
    T: int
    pv: np.ndarray                       # kW per step (t=0 = measured now)
    dem: dict                            # INFLEX -> kW per step (P50)
    tariff: np.ndarray
    grid_cap: np.ndarray                 # trunk import cap per step (0 when grid assumed down)
    day_of: np.ndarray                   # 0 = today, 1 = tomorrow ...
    soc0: float
    soc_min: float
    soc_max: float
    p_ch: float
    p_dis: float
    tank0: float
    sessions: list                       # dicts: load, a, d (relative steps), R (kWh), p_max
    burden: dict = field(default_factory=lambda: {l: 1.0 for l in INFLEX})
    peak_so_far: float = 0.0
    reserve_kwh: float = 0.0             # 0 disables the reserve constraint
    reserve_from: int = 1                # first step the reserve applies (released while islanded)
    branches: list = field(default_factory=list)
    fair_minmax: dict | None = None      # {"shed_kwh": {l}, "dem_kwh": {l}, "rs": float}
    force_p0: dict = field(default_factory=dict)   # counterfactual: load -> kW it must draw at t=0
    noshed0: list = field(default_factory=list)    # counterfactual: loads that may not be shed at t=0
    force_dis0: float = 0.0                         # counterfactual: battery must discharge >= kW at t=0


@dataclass
class LPResult:
    ok: bool
    status: str
    objective: float
    solve_ms: float
    trunk: dict                          # arrays over the horizon
    shadow_price: float                  # Rs/kWh at t=0
    binding: list
    n_vars: int
    n_rows: int
    branch_cost: list = field(default_factory=list)


class _B:
    """Tiny sparse LP assembler."""

    def __init__(self):
        self.c, self.lb, self.ub = [], [], []
        self.n = 0
        self.rows = {"eq": [0, [], [], [], []], "ub": [0, [], [], [], []]}  # nrows, r, c, v, rhs

    def var(self, n, cost=0.0, lb=0.0, ub=np.inf):
        idx = np.arange(self.n, self.n + n)
        self.n += n
        self.c.append(np.broadcast_to(np.asarray(cost, float), (n,)).copy())
        self.lb.append(np.broadcast_to(np.asarray(lb, float), (n,)).copy())
        self.ub.append(np.broadcast_to(np.asarray(ub, float), (n,)).copy())
        return idx

    def add_cost(self, idx, cost):
        self._cost_adj.append((np.atleast_1d(idx), np.broadcast_to(np.asarray(cost, float), np.atleast_1d(idx).shape)))

    def rows_(self, kind, nrows, terms, rhs):
        """terms: list of (local_row_index_array, col_index_array, value_array)."""
        R = self.rows[kind]
        base = R[0]
        for lr, cols, vals in terms:
            lr = np.atleast_1d(lr)
            R[1].append(base + lr)
            R[2].append(np.atleast_1d(cols))
            R[3].append(np.broadcast_to(np.asarray(vals, float), lr.shape))
        R[4].append(np.broadcast_to(np.asarray(rhs, float), (nrows,)))
        R[0] += nrows
        return np.arange(base, base + nrows)

    def matrices(self):
        out = {}
        for kind, R in self.rows.items():
            if R[0] == 0:
                out[kind] = (None, None)
                continue
            A = sp.csr_matrix((np.concatenate(R[3]), (np.concatenate(R[1]), np.concatenate(R[2]))),
                              shape=(R[0], self.n))
            out[kind] = (A, np.concatenate(R[4]))
        return out


def solve(p: dict, d: LPData, time_limit: float | None = None) -> LPResult:
    t0 = time.perf_counter()
    T = d.T
    b = p["battery"]
    eta_c, eta_d, wear = b["eta_charge"], b["eta_discharge"], b["wear_rs_per_kwh"]
    dg_rs = p["diesel"]["rs_per_litre"] * p["diesel"]["litres_per_kwh"]
    lpk = p["diesel"]["litres_per_kwh"]
    m = p["mpc"]
    tv = m["terminal_value_rs_per_kwh"]
    cpen = m["curtail_penalty_rs_per_kwh"]
    pc = p["grid"]["peak_charge_rs_per_kw"]
    L = p["loads"]
    soc0 = min(max(d.soc0, d.soc_min), d.soc_max)
    B = _B()
    B._cost_adj = []

    # trunk step weights
    w_tr = np.ones(T)
    for br in d.branches:
        w_tr[br.tau:br.end] -= br.weight
    dem_tot = sum(d.dem[l] for l in INFLEX)

    def segment(start, end, cap, w, soc_prev_idx):
        """Create per-step variables/constraints for owned steps [start, end)."""
        n = end - start
        sl = slice(start, end)
        ww = w if np.ndim(w) else np.full(n, w)
        v = {}
        v["grid"] = B.var(n, d.tariff[sl] * DT * ww, 0, cap)
        v["ch"] = B.var(n, wear * DT * ww, 0, d.p_ch)
        v["dis"] = B.var(n, wear * DT * ww, 0, d.p_dis)
        v["dg"] = B.var(n, dg_rs * DT * ww, 0, p["diesel"]["p_max_kw"])
        v["curt"] = B.var(n, cpen * DT * ww, 0, d.pv[sl])
        for l in INFLEX:
            v[f"shed_{l}"] = B.var(n, L[l]["voll"] * d.burden.get(l, 1.0) * DT * ww, 0, d.dem[l][sl])
        v["soc"] = B.var(n, 0.0, d.soc_min, d.soc_max)       # soc at t+1
        # session power variables (only where available)
        pv_ = {}
        for j, s in enumerate(d.sessions):
            a, e = max(start, s["a"]), min(end, s["d"], T)
            if e > a:
                pv_[j] = (np.arange(a, e), B.var(e - a, 0.0, 0, s["p_max"]))
        v["p"] = pv_
        r = np.arange(n)
        # balance: -curt + grid + dg + dis - ch - sum p + sum shed = dem - pv
        terms = [(r, v["curt"], -1), (r, v["grid"], 1), (r, v["dg"], 1), (r, v["dis"], 1), (r, v["ch"], -1)]
        terms += [(r, v[f"shed_{l}"], 1) for l in INFLEX]
        for j, (ts, idx) in pv_.items():
            terms.append((ts - start, idx, -1))
        v["bal_rows"] = B.rows_("eq", n, terms, dem_tot[sl] - d.pv[sl])
        # soc dynamics: soc[t] - soc[t-1] - eta_c dt ch + dt/eta_d dis = 0
        rhs = np.zeros(n)
        terms = [(r, v["soc"], 1), (r, v["ch"], -eta_c * DT), (r, v["dis"], DT / eta_d)]
        if n > 1:
            terms.append((r[1:], v["soc"][:-1], -1))
        if soc_prev_idx is None:
            rhs[0] = soc0
        else:
            terms.append((np.array([0]), np.array([soc_prev_idx]), -1))
        B.rows_("eq", n, terms, rhs)
        # per-load power cap when several sessions of one load overlap
        for f in FLEX:
            js = [j for j in pv_ if d.sessions[j]["load"] == f]
            if len(js) > 1:
                terms = [(pv_[j][0] - start, pv_[j][1], 1) for j in js]
                B.rows_("ub", n, terms, L[f]["p_kw"])
        return v

    # ---------------- trunk
    tr = segment(0, T, d.grid_cap, w_tr, None)
    days = np.unique(d.day_of)
    tr_peak = {}
    for dd in days:
        ts = np.where(d.day_of == dd)[0]
        lb = d.peak_so_far if dd == 0 else 0.0
        pk = B.var(1, pc, lb, np.inf)
        tr_peak[dd] = pk[0]
        B.rows_("ub", len(ts), [(np.arange(len(ts)), tr["grid"][ts], 1), (np.arange(len(ts)), np.full(len(ts), pk[0]), -1)], 0.0)
    refill = p["diesel"].get("refill_daily", False)
    full_tank = p["diesel"]["tank_litres"]

    def fuel_rows(dg_idx, steps):
        """Fuel budget per calendar day when the tank is refilled at midnight, else one budget."""
        groups = [np.where(d.day_of[steps] == dd)[0] for dd in np.unique(d.day_of[steps])] if refill else [np.arange(len(steps))]
        for g in groups:
            dd = d.day_of[steps[g[0]]]
            cap_l = d.tank0 if (not refill or dd == 0) else full_tank
            B.rows_("ub", 1, [(np.zeros(len(g), int), dg_idx[g], DT * lpk)], cap_l)

    fuel_rows(tr["dg"], np.arange(T))
    tr_slack = {}
    for j, s in enumerate(d.sessions):
        after = s["p_max"] * DT * max(0, s["d"] - T)
        need = s["R"] - after
        if need <= 1e-9:
            continue
        sk = B.var(1, L[s["load"]]["slack_rs"], 0, np.inf)[0]
        tr_slack[j] = sk
        terms = [(np.array([0]), np.array([sk]), -1)]
        if j in tr["p"]:
            terms.append((np.zeros(len(tr["p"][j][1]), int), tr["p"][j][1], -DT))
        B.rows_("ub", 1, terms, -need)
    if d.reserve_kwh > 0:
        # soc var t is the energy at step t+1; the reserve binds on steps >= reserve_from
        a0 = max(0, d.reserve_from - 1)
        gap = B.var(T, m["reserve_penalty_rs_per_kwh"] * w_tr, 0, np.inf)
        r = np.arange(T - a0)
        B.rows_("ub", T - a0, [(r, tr["soc"][a0:], -1), (r, gap[a0:], -1)], -d.reserve_kwh)
        tr["gap"] = gap
    # counterfactual forcing (explanations)
    for f, kw in d.force_p0.items():
        terms = [(np.array([0]), np.array([idx[0]]), -1) for j, (ts, idx) in tr["p"].items()
                 if d.sessions[j]["load"] == f and ts[0] == 0]
        if terms:
            B.rows_("ub", 1, terms, -kw)
    for l in d.noshed0:
        B._noshed = getattr(B, "_noshed", []) + [tr[f"shed_{l}"][0]]
    # terminal value on trunk
    B.add_cost(tr["soc"][-1], -tv)
    # min-max fairness over today's remaining steps
    if d.fair_minmax:
        fm = d.fair_minmax
        z = B.var(1, fm["rs"], 0, np.inf)[0]
        ts = np.where(d.day_of == 0)[0]
        for l in FAIR:
            D = max(fm["dem_kwh"][l], 1e-3)
            terms = [(np.zeros(len(ts), int), tr[f"shed_{l}"][ts], DT / D), (np.array([0]), np.array([z]), -1)]
            B.rows_("ub", 1, terms, -fm["shed_kwh"][l] / D)

    # ---------------- branches
    br_vars = []
    for br in d.branches:
        w = br.weight
        bv = segment(br.tau, br.end, br.cap, w, tr["soc"][br.tau - 1])
        br_vars.append(bv)
        own = np.arange(br.tau, br.end)
        # peaks: branch peak >= trunk peak, >= branch grid; pays the difference
        for dd in np.unique(d.day_of[own]):
            ts = np.where(d.day_of[own] == dd)[0]
            bp = B.var(1, w * pc, 0, np.inf)[0]
            B.add_cost(tr_peak[dd], -w * pc)
            B.rows_("ub", len(ts), [(np.arange(len(ts)), bv["grid"][ts], 1), (np.arange(len(ts)), np.full(len(ts), bp), -1)], 0.0)
            B.rows_("ub", 1, [(np.array([0]), np.array([tr_peak[dd]]), 1), (np.array([0]), np.array([bp]), -1)], 0.0)
        # fuel along the path (trunk steps before tau + branch steps), per day if refilled
        fuel_rows(np.concatenate([tr["dg"][:br.tau], bv["dg"]]), np.arange(br.end))
        # sessions
        for j, s in enumerate(d.sessions):
            if j not in bv["p"] and not (s["a"] < br.end and s["d"] > br.tau):
                continue
            tr_idx = tr["p"].get(j)
            if s["d"] <= br.end:
                # deadline inside the branch: hard requirement along this path
                need = s["R"]
                if need <= 1e-9:
                    continue
                sk = B.var(1, w * L[s["load"]]["slack_rs"], 0, np.inf)[0]
                terms = [(np.array([0]), np.array([sk]), -1)]
                if tr_idx is not None:
                    pre = tr_idx[0] < br.tau
                    if pre.any():
                        terms.append((np.zeros(pre.sum(), int), tr_idx[1][pre], -DT))
                if j in bv["p"]:
                    terms.append((np.zeros(len(bv["p"][j][1]), int), bv["p"][j][1], -DT))
                B.rows_("ub", 1, terms, -need)
            elif tr_idx is not None:
                # deadline after rejoin: energy the branch falls behind the trunk is bought back later at tv
                inside = (tr_idx[0] >= br.tau) & (tr_idx[0] < br.end)
                if not inside.any():
                    continue
                dfr = B.var(1, w * tv, 0, np.inf)[0]
                terms = [(np.zeros(inside.sum(), int), tr_idx[1][inside], DT), (np.array([0]), np.array([dfr]), -1)]
                if j in bv["p"]:
                    terms.append((np.zeros(len(bv["p"][j][1]), int), bv["p"][j][1], -DT))
                B.rows_("ub", 1, terms, 0.0)
        # rejoin: branch battery energy valued vs trunk at the rejoin step
        B.add_cost(bv["soc"][-1], -w * tv)
        B.add_cost(tr["soc"][br.end - 1], w * tv)

    # ---------------- assemble + solve
    c = np.concatenate(B.c)
    for idx, val in B._cost_adj:
        np.add.at(c, idx, val)
    bounds = np.column_stack([np.concatenate(B.lb), np.concatenate(B.ub)])
    for i in getattr(B, "_noshed", []):
        bounds[i, 1] = 0.0
    if d.force_dis0 > 0:
        bounds[tr["dis"][0], 0] = min(d.force_dis0, bounds[tr["dis"][0], 1])
    mats = B.matrices()
    A_eq, b_eq = mats["eq"]
    A_ub, b_ub = mats["ub"]
    opts = {"presolve": True}
    if time_limit:
        opts["time_limit"] = time_limit
    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs", options=opts)
    ms = (time.perf_counter() - t0) * 1000
    nrows = (A_eq.shape[0] if A_eq is not None else 0) + (A_ub.shape[0] if A_ub is not None else 0)
    if res.status != 0 or res.x is None:
        return LPResult(False, res.message, np.nan, ms, {}, np.nan, [], B.n, nrows)
    x = res.x
    trunk = {k: x[tr[k]] for k in ("grid", "ch", "dis", "dg", "curt", "soc")}
    for l in INFLEX:
        trunk[f"shed_{l}"] = x[tr[f"shed_{l}"]]
    for f in FLEX:
        arr = np.zeros(T)
        for j, (ts, idx) in tr["p"].items():
            if d.sessions[j]["load"] == f:
                arr[ts] += x[idx]
        trunk[f"p_{f}"] = arr
    if "gap" in tr:
        trunk["gap"] = x[tr["gap"]]
    duals = res.eqlin.marginals if res.eqlin is not None else None
    shadow = abs(duals[tr["bal_rows"][0]]) / DT if duals is not None else np.nan
    binding = []
    if trunk["soc"][0] <= d.soc_min + 1e-4:
        binding.append("soc_min")
    if trunk["soc"][0] >= d.soc_max - 1e-4:
        binding.append("soc_max")
    if d.grid_cap[0] > 0 and trunk["grid"][0] >= d.grid_cap[0] - 1e-4:
        binding.append("grid_cap")
    if d.grid_cap[0] <= 0:
        binding.append("islanded")
    if "gap" in trunk and trunk["gap"][0] > 1e-4:
        binding.append("reserve_gap")
    if trunk["soc"][0] <= d.reserve_kwh + 1e-3 and d.reserve_kwh > 0 and "reserve_gap" not in binding:
        binding.append("reserve_floor")
    bcost = []
    for br, bv in zip(d.branches, br_vars):
        bcost.append(float(sum(x[bv["grid"]] * d.tariff[br.tau:br.end] * DT)
                           + sum(x[bv["dg"]]) * dg_rs * DT
                           + sum(sum(x[bv[f"shed_{l}"]]) * L[l]["voll"] * DT for l in INFLEX)))
    return LPResult(True, "optimal", float(res.fun), ms, trunk, float(shadow), binding, B.n, nrows, bcost)
