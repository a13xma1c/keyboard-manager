"""The trigger engine: rules of the form "when X happens (and a condition holds),
run a macro" - beyond a macro's own hotkey, abbreviation, schedule and event.

Kinds of trigger:

* ``macro_finished``  - another macro finished (optionally: with a given result).
  Chains are limited (``MAX_CHAIN`` runs started this way in a row), so two rules
  that start each other cannot loop forever.
* ``clipboard``       - the clipboard text changed (optionally: to something
  containing a text).
* ``file``            - a file was created, changed or deleted.
* ``becomes_true``    - an expression changed from false to true, e.g.
  ``"Invoice" in window_title`` or ``counter >= 10``.

The condition, when given, is an expression checked at the moment the trigger fires
(variables: the saved ones and the built-ins). Polling kinds are checked every
second, only while such rules exist.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

POLL_S = 1.0
MAX_CHAIN = 10
KINDS = {
    "macro_finished": "Another macro finished",
    "clipboard": "The clipboard changed",
    "file": "A file changed",
    "becomes_true": "An expression becomes true",
}
FINISH_RESULTS = ("any", "success", "error", "stopped")


@dataclass
class TriggerRule:
    name: str
    kind: str
    macro: str                  # the macro to run (by name)
    params: dict[str, Any] = field(default_factory=dict)
    condition: str = ""          # optional expression, checked when the trigger fires
    queue: bool = False          # queue the run when another macro is running
    enabled: bool = True
    id: int | None = None

    def describe(self) -> str:
        p = self.params
        if self.kind == "macro_finished":
            result = p.get("result", "any")
            return f'"{p.get("macro", "")}" finished' + ("" if result == "any" else f" with {result}")
        if self.kind == "clipboard":
            return "the clipboard changes" + (f' to contain "{p["contains"]}"' if p.get("contains") else "")
        if self.kind == "file":
            return f'{p.get("path", "")} changes'
        if self.kind == "becomes_true":
            return f"{p.get('expression', '')} becomes true"
        return self.kind

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "macro": self.macro, "params": dict(self.params),
                "condition": self.condition, "queue": self.queue, "enabled": self.enabled}


def validate_rule(rule: TriggerRule) -> TriggerRule:
    """Raise ``ValueError`` for an unusable rule."""
    from keyboardmanager.core.script import check_expression

    if not rule.name.strip():
        raise ValueError("give the trigger a name")
    if rule.kind not in KINDS:
        raise ValueError(f"unknown kind of trigger {rule.kind!r}")
    if not rule.macro.strip():
        raise ValueError("choose the macro to run")
    p = rule.params
    if rule.kind == "macro_finished":
        if not str(p.get("macro", "")).strip():
            raise ValueError("choose the macro whose end starts this one")
        if p.get("result", "any") not in FINISH_RESULTS:
            raise ValueError(f"the result must be one of {', '.join(FINISH_RESULTS)}")
    elif rule.kind == "file" and not str(p.get("path", "")).strip():
        raise ValueError("choose the file to watch")
    elif rule.kind == "becomes_true":
        check_expression(str(p.get("expression", "")) or " ")
    if rule.condition.strip():
        check_expression(rule.condition)
    return rule


def rule_from_row(row: Any) -> TriggerRule:
    try:
        params = json.loads(row["params"] or "{}")
    except ValueError:
        params = {}
    return TriggerRule(name=row["name"], kind=row["kind"], macro=row["macro"], params=params,
                       condition=row["condition"], queue=bool(row["queue"]), enabled=bool(row["enabled"]),
                       id=row["id"])


class TriggerEngine:
    def __init__(
        self,
        fire: Callable[[TriggerRule, str, int], None],
        evaluate: Callable[[str], Any],
        clipboard: Callable[[], str],
        file_state: Callable[[str], Any] | None = None,
    ) -> None:
        """``fire(rule, trigger text, chain depth)`` runs the macro; ``evaluate`` an
        expression; ``clipboard`` the current clipboard text."""
        self._fire = fire
        self._evaluate = evaluate
        self._clipboard = clipboard
        self._file_state = file_state or _mtime
        self._lock = threading.Lock()
        self._rules: list[TriggerRule] = []
        self._seen: dict[int, Any] = {}  # per rule: what the last poll saw
        self.status: dict[int, str] = {}  # per rule: what happened last, for the Triggers tab
        self._thread: threading.Thread | None = None
        self._quit = threading.Event()

    def update(self, rules: list[TriggerRule], thread: bool = True) -> None:
        with self._lock:
            self._rules = [r for r in rules if r.enabled]
            ids = {r.id for r in self._rules}
            self._seen = {k: v for k, v in self._seen.items() if k in ids}
        polling = any(r.kind != "macro_finished" for r in self._rules)
        if polling and thread:
            self.start()
        elif not polling:
            self.stop()

    # ------------------------------------------------------------- triggers
    def macro_finished(self, name: str, status: str, chain: int = 0) -> None:
        """Called by the service when a run ends."""
        for rule in self._rules_of("macro_finished"):
            wanted = rule.params.get("result", "any")
            if rule.params.get("macro", "").strip().lower() != name.strip().lower():
                continue
            if wanted != "any" and wanted != status:
                continue
            if chain + 1 > MAX_CHAIN:
                self.set_status(rule, f"not started: {MAX_CHAIN} macros in a row were started by triggers")
                continue
            self._maybe_fire(rule, f'trigger "{rule.name}": {rule.describe()}', chain + 1)

    def tick(self) -> None:
        """One poll of the clipboard, files and expressions."""
        for rule in self._rules_of("clipboard", "file", "becomes_true"):
            try:
                now = self._observe(rule)
            except Exception as exc:  # noqa: BLE001
                self.set_status(rule, f"error: {exc}")
                continue
            first = rule.id not in self._seen
            before = self._seen.get(rule.id)
            self._seen[rule.id] = now
            if first:
                continue  # the first look is the baseline, not a change
            if rule.kind == "becomes_true":
                happened = now and not before
            elif rule.kind == "clipboard":
                contains = str(rule.params.get("contains", ""))
                happened = now != before and (not contains or contains.lower() in now.lower())
            else:
                happened = now != before
            if happened:
                self._maybe_fire(rule, f'trigger "{rule.name}": {rule.describe()}', 0)

    def _observe(self, rule: TriggerRule) -> Any:
        if rule.kind == "clipboard":
            return self._clipboard()
        if rule.kind == "file":
            return self._file_state(os.path.expanduser(str(rule.params.get("path", ""))))
        from keyboardmanager.core.script import truthy

        return truthy(self._evaluate(str(rule.params.get("expression", ""))))

    def _maybe_fire(self, rule: TriggerRule, text: str, chain: int) -> None:
        if rule.condition.strip():
            from keyboardmanager.core.script import truthy

            try:
                if not truthy(self._evaluate(rule.condition)):
                    self.set_status(rule, "triggered, but the condition was false")
                    return
            except Exception as exc:  # noqa: BLE001
                self.set_status(rule, f"condition error: {exc}")
                return
        self.set_status(rule, "started the macro")
        self._fire(rule, text, chain)

    def active_count(self) -> int:
        with self._lock:
            return len(self._rules)

    def set_status(self, rule: TriggerRule, text: str) -> None:
        self.status[rule.id] = f"{time.strftime('%H:%M:%S')} {text}"  # type: ignore[index]

    def _rules_of(self, *kinds: str) -> list[TriggerRule]:
        with self._lock:
            return [r for r in self._rules if r.kind in kinds]

    # --------------------------------------------------------------- thread
    def start(self) -> None:
        if self._thread is not None:
            return
        self._quit = threading.Event()  # each thread has its own: a stopped one never resumes
        self._thread = threading.Thread(target=self._loop, args=(self._quit,), daemon=True, name="triggers")
        self._thread.start()

    def stop(self) -> None:
        self._quit.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.5)  # a poll may be waiting for the GUI thread (clipboard): do not block it long

    def _loop(self, quit_: threading.Event) -> None:
        while not quit_.wait(POLL_S):
            self.tick()


def _mtime(path: str) -> tuple[int, int] | None:
    """A file's modification time and size (both: some file systems store times in
    whole seconds), ``None`` when it does not exist."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size
