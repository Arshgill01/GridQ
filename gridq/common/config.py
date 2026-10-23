"""Config loading. Everything reads params through `load_params()` so the
benchmark can hash exactly what it ran with."""
from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DT = 0.25  # hours per step (15 min)
SPD = 96   # steps per day

INFLEX = ["clinic", "hostel_a", "hostel_b", "academic"]
FLEX = ["pump", "ev", "heater"]
FAIR = ["hostel_a", "hostel_b", "academic"]
IBLEND_FEEDERS = ["hostel_a", "hostel_b", "academic"]
# Spec §10.2 bit order
BIT_ORDER = ["clinic", "pump", "hostel_a", "hostel_b", "academic", "ev", "heater"]
BIT = {name: i for i, name in enumerate(BIT_ORDER)}


@lru_cache(maxsize=None)
def _load(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_params(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    p = copy.deepcopy(_load(str(path or ROOT / "config" / "params.yaml")))
    if overrides:
        deep_update(p, overrides)
    return p


def load_hardware() -> dict:
    return copy.deepcopy(_load(str(ROOT / "config" / "hardware.yaml")))


def deep_update(base: dict, upd: dict) -> dict:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


def params_hash(p: dict) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:12]


def tariff_for_hour(p: dict, hour: float) -> tuple[float, str]:
    t = p["grid"]["tariff_rs_per_kwh"]
    if t["solar"]["from"] <= hour < t["solar"]["to"]:
        return t["solar"]["rate"], "solar"
    if t["peak"]["from"] <= hour < t["peak"]["to"]:
        return t["peak"]["rate"], "peak"
    return t["normal"]["rate"], "normal"


def tariff_vector(p: dict) -> tuple[list[float], list[str]]:
    """Per-step tariff for one day (96 values)."""
    rates, bands = [], []
    for s in range(SPD):
        r, b = tariff_for_hour(p, s * DT)
        rates.append(r)
        bands.append(b)
    return rates, bands


def mask_from_loads(names) -> int:
    m = 0
    for n in names:
        m |= 1 << BIT[n]
    return m
