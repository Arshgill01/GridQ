"""Digital twin of the campus microgrid (SIMULATED PLANT).

Every controller runs against this same physics. The twin clips controller
requests to physical limits, gates loads with the MCU's applied mask, closes the
energy balance (grid is the slack, battery is grid-forming when islanded),
auto-sheds when it cannot, and records per-step metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gridq.common.config import BIT, DT, FAIR, FLEX, INFLEX, SPD, tariff_vector
from gridq.plant.events import Event, apply_event
from gridq.plant.profiles import DayData, clinic_profile, _date_seed

BAL_TOL = 1e-6


# ------------------------------------------------------------------ data containers
@dataclass
class Timeline:
    """Concatenated true inputs for a run of days (+ lookahead days for the horizon)."""
    dates: list
    n_days: int                    # simulated days (lookahead days follow)
    demand: dict                   # load -> array (all INFLEX incl. clinic), kW
    pv: np.ndarray
    pv_clear: np.ndarray
    tariff: np.ndarray
    band: list
    weekday: np.ndarray            # per step
    grid_sched: np.ndarray         # bool per step (bench outages); all True live
    pre_dem: dict = field(default_factory=dict)   # measured history before step 0 (up to 7 days)
    pre_pv: np.ndarray = field(default_factory=lambda: np.zeros(0))
    day_cal: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))      # per day: weekday, workday
    pre_day_cal: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))

    @property
    def n_steps(self) -> int:
        return self.n_days * SPD

    @staticmethod
    def from_days(p: dict, days: list[DayData], n_sim: int | None = None,
                  grid_sched: np.ndarray | None = None, pre_days: list[DayData] | None = None) -> "Timeline":
        n_sim = len(days) - 1 if n_sim is None else n_sim
        rates, bands = tariff_vector(p)
        dem = {l: [] for l in INFLEX}
        pv, pvc, wd = [], [], []
        for d in days:
            rng = np.random.default_rng(_date_seed(d.date) + 17)
            dem["clinic"].append(clinic_profile(p, rng))
            for f in ("hostel_a", "hostel_b", "academic"):
                dem[f].append(d.demand[f])
            pv.append(d.pv)
            pvc.append(d.pv_clear)
            wd.append(np.full(SPD, d.weekday))
        n = len(days)
        gs = np.ones(n * SPD, bool) if grid_sched is None else grid_sched[: n * SPD]
        pre = list(pre_days or [])[-7:]
        pre_dem = {l: np.concatenate([_day_load(p, d, l) for d in pre]) if pre else np.zeros(0) for l in INFLEX}
        pre_pv = np.concatenate([d.pv for d in pre]) if pre else np.zeros(0)
        return Timeline(pre_dem=pre_dem, pre_pv=pre_pv,
            day_cal=np.array([[d.weekday, float(d.workday)] for d in days], float).reshape(-1, 2),
            pre_day_cal=np.array([[d.weekday, float(d.workday)] for d in pre], float).reshape(-1, 2),
            dates=[d.date for d in days], n_days=n_sim,
            demand={l: np.concatenate(v) for l, v in dem.items()},
            pv=np.concatenate(pv), pv_clear=np.concatenate(pvc),
            tariff=np.tile(rates, n), band=bands * n, weekday=np.concatenate(wd), grid_sched=gs)


def _day_load(p: dict, d: DayData, l: str) -> np.ndarray:
    if l == "clinic":
        return clinic_profile(p, np.random.default_rng(_date_seed(d.date) + 17))
    return d.demand[l]


@dataclass
class Session:
    id: str
    load: str
    start: int          # absolute step, energy may be delivered in [start, deadline)
    deadline: int
    energy: float
    remaining: float
    p_max: float
    source: str = "schedule"

    def to_dict(self) -> dict:
        return dict(id=self.id, load=self.load, start=self.start, deadline=self.deadline,
                    energy=self.energy, remaining=self.remaining, p_max=self.p_max, source=self.source)


def scheduled_sessions(p: dict, day: int) -> list[Session]:
    """The daily routine (known in advance to every controller)."""
    L = p["loads"]
    base = day * SPD
    out = []
    for name in ("pump", "heater"):
        c = L[name]
        a, b = c["window"]
        start = base + a * 4
        end = base + b * 4 if b > a else base + SPD + b * 4
        out.append(Session(f"{name}-{day}", name, start, end, c["energy_kwh"], c["energy_kwh"], c["p_kw"]))
    c = L["ev"]
    out.append(Session(f"ev-{day}", "ev", base + c["arrive"] * 4, base + SPD + c["deadline"] * 4,
                       c["energy_kwh"], c["energy_kwh"], c["p_kw"]))
    return out


@dataclass
class Control:
    served_frac: dict = field(default_factory=lambda: {l: 1.0 for l in INFLEX})
    p_flex: dict = field(default_factory=lambda: {f: 0.0 for f in FLEX})
    p_charge: float = 0.0
    p_discharge: float = 0.0
    p_dg: float = 0.0
    shed_policy: str = "priority"      # "baseline" (fixed order) | "priority" (reverse priority, fair group pro-rata)


@dataclass
class Observation:
    """What a controller may know at the start of step k (no future data)."""
    k: int
    step_of_day: int
    hour: float
    soc: float
    soc_frac: float
    cap_kwh: float
    soc_min: float
    soc_max: float
    p_ch_max: float
    p_dis_max: float
    tank_l: float
    grid_up: bool
    outage_elapsed: int
    demand_now: dict
    pv_now: float
    sessions: list
    shed_minutes: dict
    peak_today: float
    tariff_now: float


REC_FIELDS = ["grid", "ch", "dis", "dg", "pv", "curt", "soc", "tank", "tariff", "grid_up",
              "unserved", "flex_missed", "misses"] + \
             [f"dem_{l}" for l in INFLEX] + [f"srv_{l}" for l in INFLEX] + [f"p_{f}" for f in FLEX] + [f"miss_{f}" for f in FLEX]


class Twin:
    def __init__(self, p: dict, tl: Timeline, soc_init: float | None = None):
        self.p = p
        self.tl = tl
        b = p["battery"]
        self.cap0 = b["capacity_kwh"]
        self.pch0, self.pdis0 = b["p_charge_max_kw"], b["p_discharge_max_kw"]
        self.eta_c, self.eta_d = b["eta_charge"], b["eta_discharge"]
        self.cap_f = 1.0
        self.pow_f = 1.0
        self.soc = (soc_init if soc_init is not None else b["soc_init_frac"]) * self.cap0
        self.soc_start = self.soc
        self.tank = p["diesel"]["tank_litres"]
        self.k = 0
        self.software_outage = False
        self.cloud_mult, self.cloud_until = 1.0, -1
        self.sky_mult = 1.0
        self.heat_mult = 1.0
        self.sessions: list[Session] = []
        self.future: list[Session] = []
        for d in range(tl.n_days + 1):
            self.future.extend(scheduled_sessions(p, d))
        self.future.sort(key=lambda s: s.start)
        self.fleet_n = 0
        self.event_log: list[dict] = []
        self.shed_minutes = {l: 0.0 for l in INFLEX}
        self.peak_today = 0.0
        self.outage_elapsed = 0
        n = tl.n_steps
        self.rec = {f: np.zeros(n) for f in REC_FIELDS}
        self.hist_pv = np.zeros(n)          # measured (incl. events)
        self.hist_dem = {l: np.zeros(n) for l in INFLEX}
        self.max_residual = 0.0
        self.mode_log = np.zeros(n, dtype=np.int8)  # 0 normal 1 safe
        self._activate()

    # ------------------------------------------------------------- limits
    @property
    def cap(self):
        return self.cap0 * self.cap_f

    @property
    def soc_min(self):
        return self.p["battery"]["soc_min_frac"] * self.cap

    @property
    def soc_max(self):
        return self.p["battery"]["soc_max_frac"] * self.cap

    def set_fault(self, cap_factor: float, pow_factor: float):
        self.cap_f, self.pow_f = cap_factor, pow_factor
        self.soc = min(self.soc, self.soc_max)

    def add_ev_fleet(self, kwh: float, deadline_hour: float):
        now = self.k
        sod = now % SPD
        dl = (now - sod) + int(round(deadline_hour * 4))
        if dl <= now + 1:
            dl += SPD
        self.fleet_n += 1
        s = Session(f"fleet-{self.fleet_n}", "ev", now, dl, kwh, kwh, self.p["loads"]["ev"]["p_kw"], "event")
        self.sessions.append(s)

    def _activate(self):
        while self.future and self.future[0].start <= self.k:
            self.sessions.append(self.future.pop(0))

    # ------------------------------------------------------------- truth for this step
    def pv_actual(self, k: int | None = None) -> float:
        k = self.k if k is None else k
        m = self.cloud_mult if k < self.cloud_until else 1.0
        return float(self.tl.pv[k]) * m * self.sky_mult

    def demand_actual(self, k: int | None = None) -> dict:
        k = self.k if k is None else k
        d = {l: float(self.tl.demand[l][k]) for l in INFLEX}
        for h in ("hostel_a", "hostel_b"):
            d[h] *= self.heat_mult
        return d

    def measured(self, name: str, k0: int, k1: int) -> np.ndarray:
        """Measured values for absolute steps [k0, k1), k1 <= current step (never the future).
        name: an INFLEX load or "pv". Negative steps read the pre-run history; NaN where unknown."""
        assert k1 <= self.k, "attempt to read the future"
        cur = self.hist_pv if name == "pv" else self.hist_dem[name]
        pre = self.tl.pre_pv if name == "pv" else self.tl.pre_dem[name]
        out = np.full(k1 - k0, np.nan)
        if k1 > 0 and k1 > k0:
            a = max(k0, 0)
            out[a - k0:] = cur[a:k1]
        if k0 < 0:
            b = min(k1, 0)
            lo = max(k0, -len(pre))
            if b > lo:
                out[lo - k0:b - k0] = pre[len(pre) + lo:len(pre) + b]
        return out

    # ---- the "view" interface the MPC/forecasters read through (the brain rebuilds it from JSON)
    def clear(self, k0: int, k1: int) -> np.ndarray:
        """Clear-sky PV (deterministic, known ahead). Steps before the run use the same time of day."""
        idx = np.arange(k0, k1)
        return self.tl.pv_clear[np.where(idx >= 0, idx, idx % SPD)]

    def calendar(self, k0: int, k1: int) -> tuple[np.ndarray, np.ndarray]:
        """(weekday, workday) per step: the calendar is known in advance."""
        days = np.arange(k0, k1) // SPD
        out = np.zeros((len(days), 2))
        dc, pc = self.tl.day_cal, self.tl.pre_day_cal
        for d in np.unique(days):
            if 0 <= d < len(dc):
                v = dc[d]
            elif d < 0 and len(pc) >= -d:
                v = pc[d]
            else:
                ref = dc[0] if d < 0 else dc[-1]
                wd = (ref[0] + (d if d < 0 else d - len(dc) + 1)) % 7
                v = (wd, float(wd < 5))
            out[days == d] = v
        return out[:, 0], out[:, 1]

    def tariff_slice(self, k0: int, k1: int) -> np.ndarray:
        return self.tl.tariff[k0:k1]

    def future_sessions(self, k: int, T: int) -> list[dict]:
        return [s.to_dict() for s in self.future if s.start < k + T]

    def fair_today(self, k: int) -> dict:
        d0 = k - (k % SPD)
        return {l: {"shed_kwh": float(np.sum(self.rec[f"dem_{l}"][d0:k] - self.rec[f"srv_{l}"][d0:k]) * DT),
                    "dem_kwh": float(np.sum(self.rec[f"dem_{l}"][d0:k]) * DT)} for l in FAIR}

    def sched_grid_up(self) -> bool:
        return bool(self.tl.grid_sched[self.k]) and not self.software_outage

    def observe(self, grid_up: bool | None = None) -> Observation:
        gu = self.sched_grid_up() if grid_up is None else grid_up
        sod = self.k % SPD
        return Observation(
            k=self.k, step_of_day=sod, hour=sod * DT, soc=self.soc, soc_frac=self.soc / self.cap,
            cap_kwh=self.cap, soc_min=self.soc_min, soc_max=self.soc_max,
            p_ch_max=self.pch0 * self.pow_f, p_dis_max=self.pdis0 * self.pow_f, tank_l=self.tank,
            grid_up=gu, outage_elapsed=self.outage_elapsed if not gu else 0,
            demand_now=self.demand_actual(), pv_now=self.pv_actual(),
            sessions=[s.to_dict() for s in self.sessions], shed_minutes=dict(self.shed_minutes),
            peak_today=self.peak_today, tariff_now=float(self.tl.tariff[self.k]))

    def apply(self, ev: Event):
        apply_event(self, ev)

    # ------------------------------------------------------------- step
    def step(self, c: Control, applied_mask: int = 127, grid_up: bool | None = None, mode: int = 0) -> dict:
        p, k = self.p, self.k
        gu = self.sched_grid_up() if grid_up is None else bool(grid_up)
        pv = self.pv_actual()
        dem = self.demand_actual()
        on = lambda name: bool(applied_mask >> BIT[name] & 1)

        # 1-2. inflexible service, clipped and gated
        srv = {l: (min(1.0, max(0.0, c.served_frac.get(l, 1.0))) * dem[l] if on(l) else 0.0) for l in INFLEX}
        # flexible: EDF across active sessions of each load
        pf = {}
        alloc: list[tuple[Session, float]] = []
        for f in FLEX:
            want = max(0.0, c.p_flex.get(f, 0.0)) if on(f) else 0.0
            want = min(want, p["loads"][f]["p_kw"])
            act = sorted([s for s in self.sessions if s.load == f and s.start <= k < s.deadline and s.remaining > 1e-9],
                         key=lambda s: s.deadline)
            tot = 0.0
            for s in act:
                x = min(want - tot, s.p_max, s.remaining / DT)
                if x <= 0:
                    break
                alloc.append((s, x))
                tot += x
            pf[f] = tot

        # battery & DG limits
        pch_max = min(self.pch0 * self.pow_f, max(0.0, (self.soc_max - self.soc) / (self.eta_c * DT)))
        pdis_max = min(self.pdis0 * self.pow_f, max(0.0, (self.soc - self.soc_min) * self.eta_d / DT))
        ch = min(max(0.0, c.p_charge), pch_max)
        dis = min(max(0.0, c.p_discharge), pdis_max)
        if ch > 0 and dis > 0:
            net = ch - dis
            ch, dis = (net, 0.0) if net > 0 else (0.0, -net)
        dg_max = min(p["diesel"]["p_max_kw"], self.tank / (DT * p["diesel"]["litres_per_kwh"]))
        dg = min(max(0.0, c.p_dg), dg_max)

        # 4. balance
        load = sum(srv.values()) + sum(pf.values())
        grid = load + ch - pv - dg - dis
        curt = 0.0
        cap = p["grid"]["import_limit_kw"] if gu else 0.0
        if grid < 0:   # surplus: back off DG, then battery discharge, then absorb, then curtail
            s = -grid
            r = min(s, dg); dg -= r; s -= r
            r = min(s, dis); dis -= r; s -= r
            r = min(s, pch_max - ch); ch += r; s -= r
            curt = s
            grid = 0.0
        elif grid > cap:
            deficit = grid - cap
            grid = cap
            r = min(deficit, ch); ch -= r; deficit -= r
            if not gu:  # islanded: the battery inverter is grid-forming and must hold the bus
                r = min(deficit, pdis_max - dis); dis += r; deficit -= r
            if deficit > 1e-12:
                deficit = self._shed(deficit, srv, pf, alloc, c.shed_policy)
                if deficit > 1e-9:   # nothing left to shed (should not happen): curtail-equivalent residual
                    raise RuntimeError(f"cannot close balance at step {k}: {deficit}")
        # rebuild flexible allocation after shedding
        pv_used = pv - curt
        resid = pv_used + grid + dg + dis - (sum(srv.values()) + sum(pf.values()) + ch)
        self.max_residual = max(self.max_residual, abs(resid))
        assert abs(resid) < BAL_TOL, f"energy balance residual {resid} at step {k}"

        # 5-6. state update
        self.soc += (self.eta_c * ch - dis / self.eta_d) * DT
        self.soc = min(max(self.soc, self.soc_min - 1e-9), self.soc_max + 1e-9)
        self.tank -= dg * DT * p["diesel"]["litres_per_kwh"]
        self.tank = max(0.0, self.tank)
        for s, x in alloc:
            s.remaining = max(0.0, s.remaining - x * DT)

        # unserved / fairness bookkeeping
        unserved = 0.0
        for l in INFLEX:
            u = max(0.0, dem[l] - srv[l])
            unserved += u * DT
            if dem[l] > 1e-9:
                self.shed_minutes[l] += 15.0 * (u / dem[l])
        self.peak_today = max(self.peak_today, grid)

        # 7. record
        r = self.rec
        r["grid"][k], r["ch"][k], r["dis"][k], r["dg"][k] = grid, ch, dis, dg
        r["pv"][k], r["curt"][k], r["soc"][k], r["tank"][k] = pv, curt, self.soc, self.tank
        r["tariff"][k], r["grid_up"][k], r["unserved"][k] = self.tl.tariff[k], gu, unserved
        for l in INFLEX:
            r[f"dem_{l}"][k] = dem[l]
            r[f"srv_{l}"][k] = srv[l]
        for f in FLEX:
            r[f"p_{f}"][k] = pf[f]
        self.hist_pv[k] = pv
        for l in INFLEX:
            self.hist_dem[l][k] = dem[l]
        self.mode_log[k] = mode
        self.outage_elapsed = 0 if gu else self.outage_elapsed + 1

        # advance clock, close sessions at deadline
        self.k += 1
        missed, n_miss = 0.0, 0
        keep = []
        for s in self.sessions:
            if s.deadline <= self.k:
                if s.remaining > 1e-6:
                    missed += s.remaining
                    n_miss += 1
                    self.rec[f"miss_{s.load}"][k] += s.remaining
            else:
                keep.append(s)
        self.sessions = keep
        r["flex_missed"][k] = missed
        r["misses"][k] = n_miss
        if self.k % SPD == 0:
            self.shed_minutes = {l: 0.0 for l in INFLEX}
            self.peak_today = 0.0
        if self.k < self.tl.n_steps + SPD:
            self._activate()
        return {"k": k, "grid": grid, "ch": ch, "dis": dis, "dg": dg, "pv": pv, "curt": curt,
                "soc": self.soc, "served": srv, "demand": dem, "p_flex": pf, "grid_up": gu,
                "unserved_kwh": unserved, "residual": resid}

    def _shed(self, deficit, srv, pf, alloc, policy):
        """Reduce loads until the deficit closes. Mutates srv/pf/alloc. Returns remaining deficit."""
        if policy == "baseline":
            groups = [[n] for n in self.p["shed_order_baseline"]]
        else:  # reverse priority; equal-priority fair feeders shed pro-rata
            pr = {n: c["priority"] for n, c in self.p["loads"].items()}
            levels = sorted(set(pr.values()), reverse=True)
            groups = [[n for n in self.p["loads"] if pr[n] == lv] for lv in levels]
        for g in groups:
            if deficit <= 1e-12:
                break
            avail = {n: (srv[n] if n in srv else pf[n]) for n in g}
            tot = sum(avail.values())
            if tot <= 0:
                continue
            take = min(deficit, tot)
            for n in g:
                cut = take * avail[n] / tot
                if n in srv:
                    srv[n] -= cut
                else:
                    pf[n] -= cut
                    # reduce the session allocations of this load, latest deadline first
                    rem = cut
                    for i in range(len(alloc) - 1, -1, -1):
                        s, x = alloc[i]
                        if s.load != n or rem <= 0:
                            continue
                        y = min(x, rem)
                        alloc[i] = (s, x - y)
                        rem -= y
            deficit -= take
        return deficit
