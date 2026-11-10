"""Context-aware keymaps: switch the profile and/or layer with the focused window.

A context rule says "while a window matching these filters has focus, use this
profile and/or this layer". The focused window is watched (like focus events); when
it starts matching a rule, that rule's profile / layer is applied, and when no rule
matches any more, whatever was chosen by hand comes back. The first matching rule
wins, so put specific rules (an application *and* a title) above general ones.

Rules are settings (like key remaps), not part of macro exports.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Callable

from keyboardmanager.core.models import ActiveWindow
from keyboardmanager.core.profiles import normalize_profile

SETTING = "context_rules"
ENABLED_SETTING = "contexts_enabled"
POLL_INTERVAL_S = 0.4


@dataclass
class ContextRule:
    app: str = ""                # comma separated application names (substring match)
    title: str = ""              # window title substring
    profile: str | None = None   # None = leave the profile alone ("" = All profiles)
    layer: str | None = None     # None = leave the layer alone ("" = the base layer)
    enabled: bool = True

    def matches(self, window: ActiveWindow | None) -> bool:
        if window is None:
            return False
        apps = [p.strip().lower() for p in self.app.split(",") if p.strip()]
        if apps and not any(a in window.app.lower() for a in apps):
            return False
        title = self.title.strip().lower()
        return not title or title in window.title.lower()

    def describe(self) -> str:
        where = " and ".join(part for part in (
            self.app.strip() and f"the application contains {self.app.strip()!r}",
            self.title.strip() and f"the title contains {self.title.strip()!r}",
        ) if part)
        sets = ", ".join(part for part in (
            None if self.profile is None else f'profile "{self.profile or "All profiles"}"',
            None if self.layer is None else f'layer "{self.layer or "base"}"',
        ) if part)
        return f"while {where}: {sets}"

    def to_dict(self) -> dict:
        data: dict = {"app": self.app, "title": self.title}
        if self.profile is not None:
            data["profile"] = self.profile
        if self.layer is not None:
            data["layer"] = self.layer
        if not self.enabled:
            data["enabled"] = False
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ContextRule":
        if not isinstance(data, dict):
            raise ValueError("a context rule must be an object")
        for key in ("app", "title", "profile", "layer"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"'{key}' must be text")
        return cls(app=data.get("app", ""), title=data.get("title", ""),
                   profile=data.get("profile"), layer=data.get("layer"),
                   enabled=bool(data.get("enabled", True)))


def validate_rule(rule: ContextRule) -> ContextRule:
    if not rule.app.strip() and not rule.title.strip():
        raise ValueError("say which windows the rule is for (application and/or title)")
    if rule.profile is None and rule.layer is None:
        raise ValueError("choose the profile and/or the layer the rule switches to")
    if rule.profile is not None:
        rule.profile = normalize_profile(rule.profile)
    if rule.layer is not None:
        rule.layer = rule.layer.strip()
    return rule


def rules_from_json(text: str) -> list[ContextRule]:
    if not text.strip():
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("context rules must be a list")
    return [ContextRule.from_dict(d) for d in data]


def rules_to_json(rules: list[ContextRule]) -> str:
    return json.dumps([r.to_dict() for r in rules], ensure_ascii=False)


class ContextWatcher:
    """Tells the service which rule matches the focused window (None = none).

    ``apply`` is called once per focus change, with the first matching enabled rule.
    Polls only while enabled rules exist.
    """

    def __init__(
        self,
        active_window: Callable[[], ActiveWindow],
        apply: Callable[[ContextRule | None], None],
        interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._active_window = active_window
        self._apply = apply
        self._interval = interval
        self._lock = threading.Lock()
        self._rules: list[ContextRule] = []
        self._last: ActiveWindow | None = None
        self._current: ContextRule | None = None
        self._thread: threading.Thread | None = None
        self._quit = threading.Event()

    def update(self, rules: list[ContextRule], thread: bool = True) -> None:
        with self._lock:
            self._rules = [r for r in rules if r.enabled]
            self._last = None  # re-evaluate the current window against the new rules
            needed = bool(self._rules)
        if thread and needed:
            self.start()
        elif not needed:
            self.stop()

    def check(self) -> None:
        """One poll; applies the matching rule when the focused window changed."""
        window = self._active_window()
        with self._lock:
            previous, self._last = self._last, window
            rules = list(self._rules)
        if window == previous:
            return
        matching = next((r for r in rules if r.matches(window)), None)
        if matching is not self._current:
            self._current = matching
            self._apply(matching)

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None:
            return
        self._quit = threading.Event()  # each thread has its own: a stopped one never resumes
        self._last = None
        self._thread = threading.Thread(target=self._loop, args=(self._quit,), daemon=True, name="contexts")
        self._thread.start()

    def stop(self) -> None:
        self._quit.set()
        thread, self._thread = self._thread, None
        with self._lock:
            self._last = None
        if self._current is not None:  # hand control back when switching off
            self._current = None
            self._apply(None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)

    def _loop(self, quit_: threading.Event) -> None:
        while not quit_.wait(self._interval):
            try:
                self.check()
            except Exception:  # noqa: BLE001 - a lookup failure must not kill watching
                pass
