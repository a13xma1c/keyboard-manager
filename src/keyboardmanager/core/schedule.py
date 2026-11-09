"""Schedules: when a macro runs by itself (no Qt, no OS dependencies).

Times are local wall-clock times. Occurrences are converted to POSIX timestamps
(``datetime.timestamp`` applies the local DST rules), so the scheduler can compare
them with ``time.time()`` and measure lateness in real seconds: a daily 02:30 on the
night clocks spring forward simply becomes due at 03:30.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

KINDS = ("none", "once", "weekly", "interval")
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)  # Monday = 0, like ``date.weekday()``
WORKDAYS = (0, 1, 2, 3, 4)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_INTERVAL_MINUTES = 7 * 24 * 60
AT_FORMAT = "%Y-%m-%d %H:%M"


def parse_clock(text: str) -> tuple[int, int]:
    """``"9:05"`` -> ``(9, 5)``; raises ``ValueError``."""
    try:
        hour, minute = (int(p) for p in text.strip().split(":"))
    except ValueError:
        raise ValueError(f"{text!r} is not a time (HH:MM)") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{text!r} is not a time (HH:MM)")
    return hour, minute


def parse_at(text: str) -> datetime:
    try:
        return datetime.strptime(text.strip(), AT_FORMAT)
    except ValueError:
        raise ValueError(f"{text!r} is not a date and time (YYYY-MM-DD HH:MM)") from None


@dataclass(frozen=True)
class Schedule:
    """``kind`` selects which of the other fields are used:

    * ``once``     - ``at`` (``YYYY-MM-DD HH:MM``)
    * ``weekly``   - ``time`` (``HH:MM``) on each of ``days`` (Monday = 0); all seven = daily
    * ``interval`` - every ``minutes`` minutes, counted from when the schedule became
      active (application start, save, or schedules switched on)
    """

    kind: str = "none"
    at: str = ""
    time: str = ""
    days: tuple[int, ...] = ALL_DAYS
    minutes: int = 0

    @property
    def active(self) -> bool:
        return self.kind != "none"

    # ----------------------------------------------------------- validation
    def validate(self) -> "Schedule":
        """Return a normalised copy or raise ``ValueError``."""
        if self.kind == "none":
            return Schedule()
        if self.kind == "once":
            return Schedule("once", at=parse_at(self.at).strftime(AT_FORMAT))
        if self.kind == "weekly":
            hour, minute = parse_clock(self.time)
            days = tuple(sorted(set(self.days)))
            if not days:
                raise ValueError("pick at least one day for the schedule")
            if any(not isinstance(d, int) or d not in ALL_DAYS for d in days):
                raise ValueError("schedule days must be 0 (Monday) to 6 (Sunday)")
            return Schedule("weekly", time=f"{hour:02d}:{minute:02d}", days=days)
        if self.kind == "interval":
            if not isinstance(self.minutes, int) or isinstance(self.minutes, bool) or not (
                1 <= self.minutes <= MAX_INTERVAL_MINUTES
            ):
                raise ValueError(f"the interval must be 1 to {MAX_INTERVAL_MINUTES} minutes")
            return Schedule("interval", minutes=self.minutes)
        raise ValueError(f"unknown schedule kind {self.kind!r}")

    # -------------------------------------------------------- serialisation
    def to_dict(self) -> dict[str, Any]:
        if self.kind == "once":
            return {"kind": "once", "at": self.at}
        if self.kind == "weekly":
            return {"kind": "weekly", "time": self.time, "days": list(self.days)}
        if self.kind == "interval":
            return {"kind": "interval", "minutes": self.minutes}
        return {"kind": "none"}

    @classmethod
    def from_dict(cls, data: Any) -> "Schedule":
        """Parse and validate; ``None`` means no schedule. Raises ``ValueError``."""
        if data is None:
            return cls()
        if not isinstance(data, dict) or not isinstance(data.get("kind", "none"), str):
            raise ValueError("'schedule' must be an object with a 'kind'")
        days = data.get("days", list(ALL_DAYS))
        if not isinstance(days, list):
            raise ValueError("schedule 'days' must be a list")
        for key in ("at", "time"):
            if not isinstance(data.get(key, ""), str):
                raise ValueError(f"schedule '{key}' must be text")
        return cls(
            kind=data.get("kind", "none"),
            at=data.get("at", ""),
            time=data.get("time", ""),
            days=tuple(days),
            minutes=data.get("minutes", 0),
        ).validate()

    # ---------------------------------------------------------------- timing
    def next_after(self, ts: float) -> float | None:
        """Timestamp of the first occurrence strictly after ``ts`` (``None``: never).

        Not defined for ``interval`` schedules, whose phase depends on when they
        started; the scheduler handles those.
        """
        if self.kind == "once":
            due = parse_at(self.at).timestamp()
            return due if due > ts else None
        if self.kind == "weekly":
            hour, minute = parse_clock(self.time)
            day = datetime.fromtimestamp(ts).date()
            for offset in range(9):  # a week plus slack for DST edge cases
                candidate = day + timedelta(days=offset)
                if candidate.weekday() in self.days:
                    due = datetime(candidate.year, candidate.month, candidate.day, hour, minute).timestamp()
                    if due > ts:
                        return due
            return None
        return None

    # --------------------------------------------------------------- display
    def describe(self) -> str:
        if self.kind == "once":
            return f"Once on {self.at}"
        if self.kind == "weekly":
            return f"{describe_days(self.days)} at {self.time}"
        if self.kind == "interval":
            return f"Every {describe_minutes(self.minutes)}"
        return "Not scheduled"


def describe_days(days: tuple[int, ...]) -> str:
    days = tuple(sorted(days))
    if days == ALL_DAYS:
        return "Daily"
    if days == WORKDAYS:
        return "Weekdays"
    if days == (5, 6):
        return "Weekends"
    return ", ".join(DAY_NAMES[d] for d in days)


def describe_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        n = minutes // 1440
        return f"{n} day{'s' if n != 1 else ''}"
    if minutes % 60 == 0:
        n = minutes // 60
        return f"{n} hour{'s' if n != 1 else ''}"
    return f"{minutes} min"


def format_when(ts: float, now: float) -> str:
    """Short human text for an upcoming run: ``today 09:00``, ``Tue 09:00``, ``2025-03-01 09:00``."""
    when = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(now).date()
    if when.date() == today:
        return f"today {when:%H:%M}"
    if when.date() == today + timedelta(days=1):
        return f"tomorrow {when:%H:%M}"
    if date.fromordinal(today.toordinal() + 6) >= when.date():
        return f"{DAY_NAMES[when.weekday()]} {when:%H:%M}"
    return f"{when:%Y-%m-%d %H:%M}"
