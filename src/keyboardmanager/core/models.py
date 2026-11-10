"""Plain data classes shared by every layer (no Qt, no OS dependencies)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from keyboardmanager.core.limits import Limits
from keyboardmanager.core.schedule import Schedule


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ActiveWindow:
    """The window that currently has focus."""

    app: str = ""
    title: str = ""


@dataclass
class Action:
    """One step of a macro.

    ``children`` holds the body of a ``repeat`` and the *then* branch of an ``if``;
    ``else_children`` holds the *else* branch of an ``if``.
    """

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    children: list["Action"] = field(default_factory=list)
    else_children: list["Action"] = field(default_factory=list)

    def clone(self) -> "Action":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "params": dict(self.params)}
        if self.children:
            data["children"] = [c.to_dict() for c in self.children]
        if self.else_children:
            data["else_children"] = [c.to_dict() for c in self.else_children]
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "Action":
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise ValueError("an action must be an object with a 'type'")
        params = data.get("params", {})
        children = data.get("children", [])
        else_children = data.get("else_children", [])
        if not isinstance(params, dict):
            raise ValueError("'params' must be an object")
        if not isinstance(children, list) or not isinstance(else_children, list):
            raise ValueError("'children' must be a list")
        return cls(
            type=data["type"],
            params=dict(params),
            children=[cls.from_dict(c) for c in children],
            else_children=[cls.from_dict(c) for c in else_children],
        )


WHEN_BUSY = ("skip", "queue")


def _csv(text: str) -> list[str]:
    return [p.strip().lower() for p in text.split(",") if p.strip()]


@dataclass
class Macro:
    name: str
    actions: list[Action] = field(default_factory=list)
    description: str = ""
    hotkey: str = ""
    app_filter: str = ""  # comma separated application names (substring match)
    window_filter: str = ""  # window title substring
    enabled: bool = True
    schedule: Schedule = field(default_factory=Schedule)
    event: str = ""  # run on an event; see core.events.EVENTS
    abbreviation: str = ""  # text expansion: typing it runs the macro; see core.expander
    folder: str = ""  # "" = top level; nested folders are "Work/Email"
    tags: list[str] = field(default_factory=list)
    favorite: bool = False
    speed: float = 1.0  # playback speed of delays and typing (2.0 = twice as fast)
    limits: Limits = field(default_factory=Limits)
    when_busy: str = "skip"  # another macro is running: "skip" this run, or "queue" it
    profiles: list[str] = field(default_factory=list)  # restrict to these profiles ([] = all)
    layer: str = ""  # hotkey / abbreviation only work while this layer is active ("" = base)
    uid: str = ""    # stable id across devices (sync, team libraries); set when first saved
    source: str = ""  # "" = my own macro; "team:<id>" = a read-only copy from a team library
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_app_specific(self) -> bool:
        return bool(self.app_filter.strip() or self.window_filter.strip())

    @property
    def scope_label(self) -> str:
        if not self.is_app_specific:
            return "Global"
        parts = []
        if self.app_filter.strip():
            parts.append(self.app_filter.strip())
        if self.window_filter.strip():
            parts.append(f'title "{self.window_filter.strip()}"')
        return " / ".join(parts)

    def matches(self, window: ActiveWindow | None) -> bool:
        """True if the macro may run while ``window`` has focus."""
        if not self.is_app_specific:
            return True
        if window is None:
            return False
        apps = _csv(self.app_filter)
        if apps and not any(a in window.app.lower() for a in apps):
            return False
        title = self.window_filter.strip().lower()
        if title and title not in window.title.lower():
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "hotkey": self.hotkey,
            "app_filter": self.app_filter,
            "window_filter": self.window_filter,
            "enabled": self.enabled,
            **({"schedule": self.schedule.to_dict()} if self.schedule.active else {}),
            **({"event": self.event} if self.event else {}),
            **({"abbreviation": self.abbreviation} if self.abbreviation else {}),
            **({"folder": self.folder} if self.folder else {}),
            **({"tags": list(self.tags)} if self.tags else {}),
            **({"favorite": True} if self.favorite else {}),
            **({"speed": self.speed} if self.speed != 1.0 else {}),
            **({"limits": self.limits.to_dict()} if self.limits.active else {}),
            **({"when_busy": self.when_busy} if self.when_busy != "skip" else {}),
            **({"profiles": list(self.profiles)} if self.profiles else {}),
            **({"layer": self.layer} if self.layer else {}),
            "actions": [a.to_dict() for a in self.actions],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Macro":
        if not isinstance(data, dict):
            raise ValueError("a macro must be an object")
        name = data.get("name")
        if not isinstance(name, str):
            raise ValueError("a macro needs a 'name'")
        actions = data.get("actions", [])
        if not isinstance(actions, list):
            raise ValueError("'actions' must be a list")
        for key in ("description", "hotkey", "app_filter", "window_filter", "event", "abbreviation", "folder"):
            if not isinstance(data.get(key, ""), str):
                raise ValueError(f"'{key}' must be text")
        for key in ("enabled", "favorite"):
            if not isinstance(data.get(key, key == "enabled"), bool):
                raise ValueError(f"'{key}' must be true or false")
        if not isinstance(data.get("tags", []), list):
            raise ValueError("'tags' must be a list")
        macro = cls(
            name=name,
            description=data.get("description", ""),
            hotkey=data.get("hotkey", ""),
            app_filter=data.get("app_filter", ""),
            window_filter=data.get("window_filter", ""),
            enabled=data.get("enabled", True),
            schedule=Schedule.from_dict(data.get("schedule")),
            event=data.get("event", ""),
            actions=[Action.from_dict(a) for a in actions],
        )
        from keyboardmanager.core.events import validate_event
        from keyboardmanager.core.expander import normalize_abbreviation

        from keyboardmanager.core.organize import normalize_folder, normalize_tags, validate_speed

        validate_event(macro.event, macro)
        macro.abbreviation = normalize_abbreviation(data.get("abbreviation", ""))
        macro.folder = normalize_folder(data.get("folder", ""))
        macro.tags = normalize_tags(data.get("tags", []))
        macro.favorite = data.get("favorite", False)
        macro.speed = validate_speed(data.get("speed", 1.0))
        macro.limits = Limits.from_dict(data.get("limits"))
        macro.when_busy = data.get("when_busy", "skip")
        if macro.when_busy not in WHEN_BUSY:
            raise ValueError(f"'when_busy' must be one of {', '.join(WHEN_BUSY)}")
        from keyboardmanager.core.profiles import normalize_profiles

        if not isinstance(data.get("profiles", []), list):
            raise ValueError("'profiles' must be a list")
        macro.profiles = normalize_profiles(data.get("profiles", []))
        layer = data.get("layer", "")
        if not isinstance(layer, str):
            raise ValueError("'layer' must be text")
        macro.layer = " ".join(layer.split())
        return macro


@dataclass
class HistoryEntry:
    id: int
    macro_id: int | None
    macro_name: str
    trigger: str
    started_at: str
    status: str  # running | success | stopped | error | skipped
    duration_ms: int = 0
    actions_run: int = 0
    message: str = ""
