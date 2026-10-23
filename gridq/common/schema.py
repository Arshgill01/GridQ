"""io <-> brain payloads (spec §10.3).

`state_payload` (io side) packs everything the brain may know at step k: the
observation, measured history (never the future), the deterministic clear-sky
and tariff ahead, the known daily routine, and today's fairness ledger.
`BrainView` (brain side) rebuilds the view interface the MPC reads through.
"""
from __future__ import annotations

from dataclasses import asdict

import numpy as np

from gridq.common.config import FAIR, INFLEX, SPD
from gridq.plant.twin import Observation

HIST_STEPS = 8 * SPD


def _l(a) -> list:
    return [None if (x != x) else round(float(x), 4) for x in a]   # NaN -> null


def state_payload(tw, obs: Observation, T: int) -> dict:
    k = obs.k
    h0 = k - HIST_STEPS
    return {
        "step": k,
        "obs": asdict(obs),
        "hist_start": h0,
        "hist": {n: _l(tw.measured(n, h0, k)) for n in INFLEX + ["pv"]},
        "clear_start": k - 2 * SPD,
        "clear": _l(tw.clear(k - 2 * SPD, k + 2 * T)),
        "tariff": _l(tw.tariff_slice(k, k + T)),
        "future_sessions": tw.future_sessions(k, T),
        "fair_today": tw.fair_today(k),
        "events": {"software_outage": tw.software_outage, "cloud_until": tw.cloud_until,
                   "battery_fault": [tw.cap_f, tw.pow_f], "sky_mult": tw.sky_mult, "heat_mult": tw.heat_mult},
    }


class BrainView:
    def __init__(self, st: dict):
        self.st = st
        self.k = st["step"]
        self.h0 = st["hist_start"]
        self.hist = {n: np.array([np.nan if v is None else v for v in a], float) for n, a in st["hist"].items()}
        self.c0 = st["clear_start"]
        self.clr = np.array(st["clear"], float)
        self.tar = np.array(st["tariff"], float)

    def measured(self, name, k0, k1):
        assert k1 <= self.k, "attempt to read the future"
        out = np.full(k1 - k0, np.nan)
        a, b = max(k0, self.h0), k1
        if b > a:
            out[a - k0:b - k0] = self.hist[name][a - self.h0:b - self.h0]
        return out

    def clear(self, k0, k1):
        idx = np.clip(np.arange(k0, k1) - self.c0, 0, len(self.clr) - 1)
        return self.clr[idx]

    def tariff_slice(self, k0, k1):
        return self.tar[k0 - self.k:k1 - self.k]

    def future_sessions(self, k, T):
        return [s for s in self.st["future_sessions"] if s["start"] < k + T]

    def fair_today(self, k):
        return self.st["fair_today"]

    def obs(self) -> Observation:
        return Observation(**self.st["obs"])
