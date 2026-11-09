"""Plain data classes shared by every layer (no Qt, no OS dependencies)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
        for key in ("description", "hotkey", "app_filter", "window_filter"):
            if not isinstance(data.get(key, ""), str):
                raise ValueError(f"'{key}' must be text")
        if not isinstance(data.get("enabled", True), bool):
            raise ValueError("'enabled' must be true or false")
        return cls(
            name=name,
            description=data.get("description", ""),
            hotkey=data.get("hotkey", ""),
            app_filter=data.get("app_filter", ""),
            window_filter=data.get("window_filter", ""),
            enabled=data.get("enabled", True),
            schedule=Schedule.from_dict(data.get("schedule")),
            actions=[Action.from_dict(a) for a in actions],
        )


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
