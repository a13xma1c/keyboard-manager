"""Profiles: named sets of macros to switch between (Work, Gaming, Writing...).

A macro lists the profiles it belongs to; an empty list means "in every profile".
Exactly one profile is active at a time ("" = *All profiles*, the default, in which
every macro is active). Macros outside the active profile are completely inactive:
their hotkeys, abbreviations, schedules, events and trigger rules do nothing, but
they can still be run by hand. The active profile is chosen in the toolbar, by the
"Switch profile" step, or automatically by a context rule (see ``core.context``).
"""

from __future__ import annotations

import json
import threading
from typing import Callable, Iterable

PROFILES_SETTING = "profiles"
ACTIVE_PROFILE_SETTING = "active_profile"
ALL_PROFILES = ""  # the default "profile": every macro is active


def normalize_profile(name: str) -> str:
    """A profile name: trimmed, without commas (the editor's field is comma separated)."""
    if not isinstance(name, str):
        raise ValueError("a profile name must be text")
    name = " ".join(name.split())
    if "," in name:
        raise ValueError("a profile name cannot contain a comma")
    return name


def normalize_profiles(value: Iterable[str] | str) -> list[str]:
    """A macro's profiles: unique (case-insensitive), in the given order.
    A string is split on commas (what the editor's field holds)."""
    if isinstance(value, str):
        value = value.split(",")
    out: list[str] = []
    for name in value:
        name = normalize_profile(name)
        if name and name.lower() not in [p.lower() for p in out]:
            out.append(name)
    return out


def in_profile(macro_profiles: list[str], active: str) -> bool:
    """Whether a macro restricted to ``macro_profiles`` is active in ``active``."""
    if not macro_profiles or active == ALL_PROFILES:
        return True
    return active.lower() in [p.lower() for p in macro_profiles]


class ProfileStore:
    """The profiles the user created; macros' profile lists always count as existing."""

    def __init__(self, load: Callable[[], str], save: Callable[[str], None]) -> None:
        self._load, self._save = load, save
        self._lock = threading.Lock()

    def all(self, macro_profiles: Iterable[str] = ()) -> list[str]:
        with self._lock:
            names = self._read()
        for name in macro_profiles:
            if name and name.lower() not in [p.lower() for p in names]:
                names.append(name)
        return sorted(names, key=str.lower)

    def add(self, name: str) -> str:
        name = normalize_profile(name)
        if not name:
            raise ValueError("please give the profile a name")
        with self._lock:
            names = self._read()
            if name.lower() in [p.lower() for p in names]:
                raise ValueError(f'there is already a profile named "{name}"')
            self._write([*names, name])
        return name

    def rename(self, old: str, new: str) -> str:
        new = normalize_profile(new)
        if not new:
            raise ValueError("please give the profile a name")
        with self._lock:
            names = self._read()
            if new.lower() != old.lower() and new.lower() in [p.lower() for p in names]:
                raise ValueError(f'there is already a profile named "{new}"')
            self._write([new if p.lower() == old.lower() else p for p in names])
        return new

    def replace(self, names: list[str]) -> None:
        """The whole list at once (what a sync brings in)."""
        with self._lock:
            self._write(normalize_profiles([n for n in names if isinstance(n, str)]))

    def stored(self) -> list[str]:
        """Only the profiles created by hand (not those just named by macros)."""
        with self._lock:
            return self._read()

    def remove(self, name: str) -> None:
        with self._lock:
            self._write([p for p in self._read() if p.lower() != name.lower()])

    def _read(self) -> list[str]:
        try:
            data = json.loads(self._load() or "[]")
        except ValueError:
            return []
        return normalize_profiles([p for p in data if isinstance(p, str)])

    def _write(self, names: list[str]) -> None:
        self._save(json.dumps(names, ensure_ascii=False))
