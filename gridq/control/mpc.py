"""GridQ MPC (spec §7.4) and the perfect-foresight Oracle (§7.5).

Each step: forecast -> build LP -> solve -> apply only the first step. If a solve
fails, the previous plan's next step is used (plans carry the full horizon).
"""
from __future__ import annotations

import numpy as np

from gridq.common.config import BIT, DT, FAIR, FLEX, INFLEX, SPD, mask_from_loads
from gridq.control.base import Controller, make_control
from gridq.control import lp
from gridq.forecast.solar import forecast_pv
from gridq.plant.outages import hazard_per_step


class MPC(Controller):
    name = "GridQ"
    shed_policy = "priority"
    oracle = False

    def __init__(self, p, forecaster=None):
        super().__init__(p)
        if forecaster is None and not self.oracle:
            from gridq.forecast.demand import load_forecaster
            forecaster = load_forecaster(p)
        self.fc = forecaster
        self.hazard = hazard_per_step(p)
        self.reset()

    def reset(self):
        self.plan = None          # (k0, LPResult, LPData, extras)
        self.solve_ms = []
        self.fail = 0

    # ------------------------------------------------------------------ inputs
    def forecasts(self, obs, tw, T):
        k = obs.k
        p50, p90 = self.fc.forecast(tw, k, T, obs.demand_now)
        pv = forecast_pv(tw, k, T, obs.pv_now)
        return p50, p90, pv

    def grid_cap(self, obs, tw, T):
        lim = self.p["grid"]["import_limit_kw"]
        cap = np.full(T, float(lim))
        if not obs.grid_up:
            cap[: max(1, self.p["mpc"]["outage_assumed_remaining_steps"])] = 0.0
        return cap

    def sessions(self, obs, tw, T):
        k = obs.k
        out = []
        for s in obs.sessions:
            if s["remaining"] > 1e-9:
                out.append(dict(load=s["load"], a=max(0, s["start"] - k), d=s["deadline"] - k,
                                R=s["remaining"], p_max=s["p_max"], id=s["id"]))
        for s in tw.future:
            if s.start >= k + T:
                break
            out.append(dict(load=s.load, a=s.start - k, d=s.deadline - k, R=s.energy, p_max=s.p_max, id=s.id))
        return out

    def branches(self, obs, T):
        sc = self.p["mpc"]["scenarios"]
        if not sc.get("enabled") or not obs.grid_up:
            return []
        lim = self.p["grid"]["import_limit_kw"]
        starts = [s for s in sc["branch_starts"] if s < T]
        haz = self.hazard[(obs.k + np.arange(T + 1)) % SPD]
        cum = np.concatenate([[0.0], np.cumsum(haz)])
        out = []
        for i, a in enumerate(starts):
            b = starts[i + 1] if i + 1 < len(starts) else min(T, a * 2)
            surv = np.exp(-cum[a])
            pr = surv * (1 - np.exp(-(cum[b] - cum[a])))
            if pr < 1e-4:
                continue
            for dur in sc["durations_steps"]:
                end = min(T, a + sc["max_branch_steps"])
                cap = np.full(end - a, float(lim))
                cap[:min(dur, end - a)] = 0.0
                out.append(lp.Branch(tau=a, end=end, cap=cap, weight=pr / len(sc["durations_steps"])))
        return out

    # ------------------------------------------------------------------ LP data
    def build(self, obs, tw):
        p, m = self.p, self.p["mpc"]
        T = m["horizon_steps"]
        k = obs.k
        p50, p90, pv = self.forecasts(obs, tw, T)
        day_of = ((k % SPD) + np.arange(T)) // SPD
        burden = {l: 1.0 for l in INFLEX}
        fair_minmax = None
        if m["fairness"] == "burden":
            for l in FAIR:
                burden[l] = 1.0 + m["fairness_burden_per_hour"] * obs.shed_minutes[l] / 60.0
        elif m["fairness"] == "minmax":
            d0 = k - (k % SPD)
            shed = {l: float(np.sum(tw.rec[f"dem_{l}"][d0:k] - tw.rec[f"srv_{l}"][d0:k]) * DT) for l in FAIR}
            today = day_of == 0
            dem = {l: float(np.sum(tw.rec[f"dem_{l}"][d0:k]) * DT + np.sum(p50[l][today]) * DT) for l in FAIR}
            fair_minmax = {"shed_kwh": shed, "dem_kwh": dem, "rs": m["fairness_minmax_rs"]}
        reserve = 0.0
        if m["reserve_mode"] == "p90" and not self.oracle:
            H = int(m["reserve_hours_clinic"] / DT)
            reserve = float(np.mean(p90["clinic"][:H])) * m["reserve_hours_clinic"]
        data = lp.LPData(
            T=T, pv=pv, dem={l: np.maximum(p50[l], 0.0) for l in INFLEX}, tariff=tw.tl.tariff[k:k + T],
            grid_cap=self.grid_cap(obs, tw, T), day_of=day_of,
            soc0=obs.soc, soc_min=obs.soc_min, soc_max=obs.soc_max, p_ch=obs.p_ch_max, p_dis=obs.p_dis_max,
            tank0=obs.tank_l, sessions=self.sessions(obs, tw, T), burden=burden,
            peak_so_far=obs.peak_today, reserve_kwh=reserve,
            branches=[] if self.oracle else self.branches(obs, T), fair_minmax=fair_minmax)
        return data, {"p50": p50, "p90": p90, "pv": pv}

    # ------------------------------------------------------------------ decide
    def decide(self, obs, tw):
        data, fc = self.build(obs, tw)
        res = lp.solve(self.p, data, time_limit=self.p["mpc"]["solve_timeout_s"])
        self.solve_ms.append(res.solve_ms)
        if res.ok:
            self.plan = (obs.k, res, data, fc)
            off = 0
        else:
            self.fail += 1
            if self.plan is None or obs.k - self.plan[0] >= data.T:
                from gridq.control.safe import Safe
                return Safe(self.p).decide(obs)
            off = obs.k - self.plan[0]
        return plan_control(self.plan[1], self.plan[2], off, obs)


def plan_control(res, data, off, obs):
    tr = res.trunk
    served = {}
    for l in INFLEX:
        d = data.dem[l][off]
        served[l] = 1.0 if d <= 1e-9 else float(np.clip(1 - tr[f"shed_{l}"][off] / d, 0, 1))
    pf = {f: float(tr[f"p_{f}"][off]) for f in FLEX}
    return make_control(pf, float(tr["ch"][off]), float(tr["dis"][off]), float(tr["dg"][off]), served=served,
                        policy="priority")


def plan_mask(res, data, off=0) -> int:
    """Spec §8: bit = 1 if served_frac > 0.05 (inflexible) or p > 0.05 p_max (flexible)."""
    from gridq.common.config import load_params
    tr = res.trunk
    m = 0
    for l in INFLEX:
        d = data.dem[l][off]
        frac = 1.0 if d <= 1e-9 else 1 - tr[f"shed_{l}"][off] / d
        if frac > 0.05:
            m |= 1 << BIT[l]
    return m


class Oracle(MPC):
    """Same LP, given the true future (demand, PV incl. cloud events, outage schedule). Benchmark only."""
    name = "Oracle"
    oracle = True

    def forecasts(self, obs, tw, T):
        ks = range(obs.k, obs.k + T)
        dem = {l: np.array([tw.demand_actual(kk)[l] for kk in ks]) for l in INFLEX}
        pv = np.array([tw.pv_actual(kk) for kk in ks])
        return dem, dem, pv

    def grid_cap(self, obs, tw, T):
        lim = self.p["grid"]["import_limit_kw"]
        g = tw.tl.grid_sched[obs.k:obs.k + T].astype(float) * lim
        if tw.software_outage:
            g[:] = 0.0
        g[0] = lim if obs.grid_up else 0.0
        return g
