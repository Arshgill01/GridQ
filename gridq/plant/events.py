"""Event types (spec §6.5) and their application to the twin."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

EVENT_TYPES = {"grid_outage", "cloud", "ev_fleet", "battery_fault", "live_sky", "heat"}

DEFAULTS = {
    "grid_outage": {"on": True},
    "cloud": {"factor": 0.3, "duration_steps": 8},
    "ev_fleet": {"kwh": 30, "deadline_hour": 6},
    "battery_fault": {"capacity_factor": 0.5, "power_factor": 0.5},
    "live_sky": {"multiplier": 1.0},
    "heat": {"multiplier": 1.0},
}


@dataclass
class Event:
    step: int
    type: str
    params: dict = field(default_factory=dict)
    source: str = "api"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def make(step: int, type: str, params: dict | None = None, source: str = "api") -> "Event":
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type}")
        merged = dict(DEFAULTS[type])
        merged.update(params or {})
        return Event(step=step, type=type, params=merged, source=source)


def apply_event(twin, ev: Event) -> None:
    t, pr = ev.type, ev.params
    if t == "grid_outage":
        twin.software_outage = bool(pr.get("on", True))
    elif t == "cloud":
        twin.cloud_mult = float(pr["factor"])
        twin.cloud_until = twin.k + int(pr["duration_steps"])
    elif t == "ev_fleet":
        twin.add_ev_fleet(float(pr["kwh"]), float(pr["deadline_hour"]))
    elif t == "battery_fault":
        twin.set_fault(float(pr["capacity_factor"]), float(pr["power_factor"]))
    elif t == "live_sky":
        twin.sky_mult = min(1.0, max(0.0, float(pr["multiplier"])))
    elif t == "heat":
        twin.heat_mult = min(1.4, max(0.8, float(pr["multiplier"])))
    twin.event_log.append(ev.to_dict())
