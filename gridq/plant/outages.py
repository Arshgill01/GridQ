"""Seeded outage schedule: Poisson count per day, uniform start, uniform duration."""
from __future__ import annotations

import numpy as np

from gridq.common.config import SPD


def day_outages(p: dict, seed: int) -> list[tuple[int, int]]:
    """Return [(start_step_of_day, n_steps)] for one day. Outages may run past midnight."""
    o = p["outages"]
    rng = np.random.default_rng(seed)
    n = rng.poisson(o["per_day_lambda"])
    out = []
    for _ in range(n):
        h0, h1 = o["start_hour_range"]
        start = int(round(rng.uniform(h0, h1) * 4))
        d0, d1 = o["duration_min_range"]
        dur = max(1, int(round(rng.uniform(d0, d1) / 15)))
        out.append((start, dur))
    return sorted(out)


def grid_schedule(p: dict, n_days: int, seed: int, extra_days: int = 1) -> np.ndarray:
    """Boolean grid_up per step over n_days (+ lookahead days, which get outages too)."""
    total = (n_days + extra_days) * SPD
    up = np.ones(total, dtype=bool)
    for d in range(n_days + extra_days):
        for start, dur in day_outages(p, seed * 100003 + d):
            a = d * SPD + start
            up[a:min(total, a + dur)] = False
    return up


def hazard_per_step(p: dict) -> np.ndarray:
    """Outage-start rate per step-of-day implied by the generator (used by scenario MPC:
    GridQ knows the outage *statistics*, never the realised schedule)."""
    o = p["outages"]
    h0, h1 = o["start_hour_range"]
    rate = np.zeros(SPD)
    n = (h1 - h0) * 4
    rate[int(h0 * 4):int(h1 * 4)] = o["per_day_lambda"] / n
    return rate
