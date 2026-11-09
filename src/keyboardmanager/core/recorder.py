"""Recording of keyboard and mouse input into macro actions.

``build_actions`` is a pure function (raw timestamped events -> compact actions) so
the interesting logic is testable without any input devices. ``Recorder`` is the
thin pynput listener wrapper that produces the raw events.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from keyboardmanager.core import keys
from keyboardmanager.core.actions import new_action
from keyboardmanager.core.models import Action

# Raw event: (timestamp_seconds, kind, data)
#   key_down / key_up : data = canonical key name
#   move              : data = (x, y)
#   click             : data = (x, y, button, pressed)
#   scroll            : data = (x, y, dx, dy)
Event = tuple[float, str, object]

DOUBLE_CLICK_S = 0.4
TEXT_GAP_S = 1.0
SCROLL_GAP_S = 0.3
CLICK_SLOP_PX = 5
MOVE_SAMPLE_S = 0.05


@dataclass
class RecordOptions:
    record_moves: bool = False
    record_delays: bool = True
    min_delay_ms: int = 50
    stop_key: str = "f9"


class _Builder:
    def __init__(self, opts: RecordOptions) -> None:
        self.o = opts
        self.out: list[Action] = []
        self.text = ""
        self.text_last_t = 0.0
        self.last_t: float | None = None
        self.mods: dict[str, dict[str, bool]] = {}  # name -> {used, down_emitted}
        self.down_keys: set[str] = set()
        self.consumed: set[str] = set()
        self.press: tuple[float, int, int, str] | None = None
        self.last_click: tuple[float, int, int, str, Action] | None = None
        self.last_scroll: tuple[float, Action] | None = None
        self.last_move_t = -1.0

    # -- helpers
    def flush_text(self) -> None:
        if self.text:
            a = new_action("type_text")
            a.params["text"] = self.text
            self.out.append(a)
            self.text = ""

    def emit(self, action: Action) -> None:
        self.flush_text()
        self.out.append(action)

    def delay(self, t: float) -> None:
        """Insert a delay for the idle time since the previous event."""
        if self.o.record_delays and self.last_t is not None:
            ms = int(round((t - self.last_t) * 1000))
            if ms >= self.o.min_delay_ms:
                a = new_action("delay")
                a.params["ms"] = ms
                self.emit(a)
        self.last_t = t

    def key_press(self, combo: str) -> Action:
        a = new_action("key_press")
        a.params["keys"] = combo
        return a

    # -- events
    def on_key_down(self, t: float, key: str) -> None:
        if key in self.down_keys:  # auto-repeat
            return
        self.down_keys.add(key)
        if key in keys.MODIFIERS:
            self.mods[key] = {"used": False, "down_emitted": False}
            return
        active = [m for m in keys.MODIFIERS if m in self.mods]
        for m in active:
            self.mods[m]["used"] = True
        self.consumed.add(key)
        hidden = [m for m in active if not self.mods[m]["down_emitted"]]
        printable = len(key) == 1 or key in ("space", "plus")
        text_char = " " if key == "space" else "+" if key == "plus" else key
        only_shift = all(m == "shift" for m in hidden)
        if printable and only_shift and len(hidden) == len(active):
            # Typing: shift only changes the character, which we already received.
            if self.text and t - self.text_last_t > TEXT_GAP_S:
                self.flush_text()
            if not self.text:
                self.delay(t)
            self.last_t = t
            self.text += text_char
            self.text_last_t = t
            return
        self.delay(t)
        self.emit(self.key_press("+".join([*hidden, key])))

    def on_key_up(self, t: float, key: str) -> None:
        self.down_keys.discard(key)
        if key in self.consumed:
            self.consumed.discard(key)
            return
        state = self.mods.pop(key, None)
        if state is None:
            return
        if state["down_emitted"]:
            self.delay(t)
            a = new_action("key_up")
            a.params["key"] = key
            self.emit(a)
        elif not state["used"]:
            self.delay(t)
            self.emit(self.key_press(key))

    def _hold_modifiers(self, t: float) -> None:
        """Mouse input while a modifier is held (shift+click): emit explicit key_down."""
        for m in keys.MODIFIERS:
            state = self.mods.get(m)
            if state and not state["down_emitted"]:
                self.delay(t)
                a = new_action("key_down")
                a.params["key"] = m
                self.emit(a)
                state["down_emitted"] = True
                state["used"] = True

    def on_move(self, t: float, x: int, y: int) -> None:
        if not self.o.record_moves or t - self.last_move_t < MOVE_SAMPLE_S:
            return
        self.last_move_t = t
        self.delay(t)
        a = new_action("mouse_move")
        a.params.update(x=x, y=y)
        self.emit(a)

    def on_click(self, t: float, x: int, y: int, button: str, pressed: bool) -> None:
        if button not in ("left", "right", "middle"):
            return
        if pressed:
            self._hold_modifiers(t)
            self.press = (t, x, y, button)
            return
        press, self.press = self.press, None
        if press is None:
            return
        pt, px, py, pbutton = press
        if pbutton == button and abs(px - x) <= CLICK_SLOP_PX and abs(py - y) <= CLICK_SLOP_PX:
            lc = self.last_click
            if (
                lc and lc[3] == button and pt - lc[0] <= DOUBLE_CLICK_S
                and abs(lc[1] - px) <= CLICK_SLOP_PX and abs(lc[2] - py) <= CLICK_SLOP_PX
                and self.out and self.out[-1] is lc[4] and not self.text
            ):
                lc[4].params["count"] += 1
                self.last_click = (t, lc[1], lc[2], button, lc[4])
                self.last_t = t
                return
            self.delay(pt)
            a = new_action("mouse_click")
            a.params.update(button=button, state="click", count=1, move_to=True, x=px, y=py)
            self.emit(a)
            self.last_click = (t, px, py, button, a)
            self.last_t = t
            return
        # drag: explicit press at the start and release at the end
        self.delay(pt)
        down = new_action("mouse_click")
        down.params.update(button=pbutton, state="press", move_to=True, x=px, y=py)
        self.emit(down)
        self.delay(t)
        up = new_action("mouse_click")
        up.params.update(button=button, state="release", move_to=True, x=x, y=y)
        self.emit(up)
        self.last_click = None

    def on_scroll(self, t: float, dx: int, dy: int) -> None:
        ls = self.last_scroll
        if ls and t - ls[0] <= SCROLL_GAP_S and self.out and self.out[-1] is ls[1] and not self.text:
            ls[1].params["dx"] += dx
            ls[1].params["dy"] += dy
            self.last_scroll = (t, ls[1])
            self.last_t = t
            return
        self._hold_modifiers(t)
        self.delay(t)
        a = new_action("mouse_scroll")
        a.params.update(dx=dx, dy=dy)
        self.emit(a)
        self.last_scroll = (t, a)


def build_actions(events: list[Event], options: RecordOptions | None = None) -> list[Action]:
    b = _Builder(options or RecordOptions())
    for t, kind, data in events:
        if kind == "key_down":
            b.on_key_down(t, data)  # type: ignore[arg-type]
        elif kind == "key_up":
            b.on_key_up(t, data)  # type: ignore[arg-type]
        elif kind == "move":
            b.on_move(t, *data)  # type: ignore[misc]
        elif kind == "click":
            b.on_click(t, *data)  # type: ignore[misc]
        elif kind == "scroll":
            _, _, dx, dy = data  # type: ignore[misc]
            b.on_scroll(t, dx, dy)
    # Modifiers still held when recording ended: release them explicitly.
    for m, state in list(b.mods.items()):
        if state["down_emitted"]:
            a = new_action("key_up")
            a.params["key"] = m
            b.emit(a)
    b.flush_text()
    return b.out


class Recorder:
    """Listens to the real keyboard and mouse until ``stop_key`` is pressed."""

    def __init__(
        self,
        options: RecordOptions | None = None,
        on_stopped: Callable[[], None] | None = None,
        on_event: Callable[[int], None] | None = None,
    ) -> None:
        self.options = options or RecordOptions()
        self._on_stopped = on_stopped
        self._on_event = on_event
        self._events: list[Event] = []
        self._lock = threading.Lock()
        self._kb = None
        self._mouse = None
        self._ctrl_held = 0
        self._stop_key_down = False
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        from pynput import keyboard, mouse

        if self._active:
            return
        self._events = []
        self._active = True
        self._kb = keyboard.Listener(on_press=self._press, on_release=self._release)
        self._mouse = mouse.Listener(
            on_move=self._move, on_click=self._click, on_scroll=self._scroll
        )
        self._kb.start()
        self._mouse.start()

    def stop(self) -> list[Action]:
        """Stop listening and return the recorded actions."""
        if self._active:
            self._active = False
            for listener in (self._kb, self._mouse):
                if listener is not None:
                    listener.stop()
        with self._lock:
            events = list(self._events)
        return build_actions(events, self.options)

    # -- listener callbacks (run on pynput threads)
    def _add(self, kind: str, data: object) -> None:
        if not self._active:
            return
        with self._lock:
            self._events.append((time.monotonic(), kind, data))
            n = len(self._events)
        if self._on_event:
            self._on_event(n)

    def _name(self, key) -> str | None:
        from keyboardmanager.core.backend import pynput_key_to_name

        return pynput_key_to_name(key, ctrl_held=self._ctrl_held > 0)

    def _press(self, key) -> None:
        name = self._name(key)
        if name is None:
            return
        if name == self.options.stop_key:
            if not self._stop_key_down:
                self._stop_key_down = True
                self._active_stop()
            return
        if name == "ctrl":
            self._ctrl_held += 1
        self._add("key_down", name)

    def _release(self, key) -> None:
        name = self._name(key)
        if name is None:
            return
        if name == self.options.stop_key:
            self._stop_key_down = False
            return
        if name == "ctrl":
            self._ctrl_held = max(0, self._ctrl_held - 1)
        self._add("key_up", name)

    def _active_stop(self) -> None:
        if self._on_stopped:
            self._on_stopped()

    def _move(self, x, y) -> None:
        self._add("move", (int(x), int(y)))

    def _click(self, x, y, button, pressed) -> None:
        self._add("click", (int(x), int(y), button.name, bool(pressed)))

    def _scroll(self, x, y, dx, dy) -> None:
        self._add("scroll", (int(x), int(y), int(dx), int(dy)))
