"""Controller interface + helpers shared by the rule-based controllers.

Rule-based controllers react to instantaneous measurements (as a real inverter
does): `obs.demand_now` and `obs.pv_now` are the current step's measured values.
"""
from __future__ import annotations

from gridq.common.config import DT, FLEX, INFLEX
from gridq.plant.twin import Control, Observation


class Controller:
    name = "base"
    shed_policy = "priority"

    def __init__(self, p: dict):
        self.p = p

    def decide(self, obs: Observation, ctx) -> Control:  # pragma: no cover
        raise NotImplementedError

    def reset(self):
        pass


def active_remaining(obs: Observation, load: str) -> float:
    """Remaining kWh of sessions of `load` that are open at this step."""
    return sum(s["remaining"] for s in obs.sessions
               if s["load"] == load and s["start"] <= obs.k < s["deadline"])


def earliest_session(obs: Observation, load: str):
    act = [s for s in obs.sessions if s["load"] == load and s["start"] <= obs.k < s["deadline"]
           and s["remaining"] > 1e-9]
    return min(act, key=lambda s: s["deadline"]) if act else None


def in_hours(h: float, a: float, b: float) -> bool:
    """h in [a, b) on a 24 h clock, wrapping past midnight when b <= a."""
    return a <= h < b if a < b else (h >= a or h < b)


def run_full(obs: Observation, p: dict, load: str) -> float:
    rem = active_remaining(obs, load)
    return min(p["loads"][load]["p_kw"], rem / DT) if rem > 1e-9 else 0.0


def battery_limits(obs: Observation, p: dict, floor_frac: float | None = None) -> tuple[float, float]:
    """(max charge kW, max discharge kW) this step, optionally with a discharge floor."""
    b = p["battery"]
    ch = min(obs.p_ch_max, max(0.0, (obs.soc_max - obs.soc) / (b["eta_charge"] * DT)))
    floor = obs.soc_min if floor_frac is None else max(obs.soc_min, floor_frac * obs.cap_kwh)
    dis = min(obs.p_dis_max, max(0.0, (obs.soc - floor) * b["eta_discharge"] / DT))
    return ch, dis


def dg_limit(obs: Observation, p: dict) -> float:
    d = p["diesel"]
    return min(d["p_max_kw"], obs.tank_l / (DT * d["litres_per_kwh"]))


def make_control(p_flex: dict, ch=0.0, dis=0.0, dg=0.0, served=None, policy="priority") -> Control:
    return Control(served_frac=served or {l: 1.0 for l in INFLEX},
                   p_flex={f: p_flex.get(f, 0.0) for f in FLEX},
                   p_charge=ch, p_discharge=dis, p_dg=dg, shed_policy=policy)
