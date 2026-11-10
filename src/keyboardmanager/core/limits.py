"""Per-macro limits: how long / how much a run may do, and how often triggers may start it."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

MANUAL_TRIGGERS = ("manual", "test run", "favorite", "debug")  # started on purpose: not limited
_RANGES = {"max_seconds": 86_400, "max_steps": 1_000_000, "cooldown_s": 86_400, "max_runs_per_day": 100_000}


@dataclass(frozen=True)
class Limits:
    max_seconds: int = 0       # stop a run after this much running time (paused time excluded)
    max_steps: int = 0         # stop a run after this many steps
    cooldown_s: int = 0        # a trigger is ignored until this long after the previous run
    max_runs_per_day: int = 0  # triggers may start it at most this often per day
    # 0 = no limit, everywhere

    @property
    def active(self) -> bool:
        return any(asdict(self).values())

    def to_dict(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if v}

    @classmethod
    def from_dict(cls, data: Any) -> "Limits":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("'limits' must be an object")
        unknown = set(data) - set(_RANGES)
        if unknown:
            raise ValueError(f"unknown limit {sorted(unknown)[0]!r}")
        values = {}
        for name, maximum in _RANGES.items():
            value = data.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"limit '{name}' must be a whole number from 0 to {maximum}")
            values[name] = value
        return cls(**values)

    def describe(self) -> str:
        parts = []
        if self.max_seconds:
            parts.append(f"stops after {self.max_seconds} s")
        if self.max_steps:
            parts.append(f"stops after {self.max_steps} steps")
        if self.cooldown_s:
            parts.append(f"at most once every {self.cooldown_s} s")
        if self.max_runs_per_day:
            parts.append(f"at most {self.max_runs_per_day} times a day")
        return ", ".join(parts) or "no limits"
