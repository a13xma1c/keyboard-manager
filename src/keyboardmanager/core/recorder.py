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
#   key_down          : data = canonical key name, or (name, typed character)
#   key_up            : data = canonical key name
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
    pause_key: str = "f8"  # pauses / resumes capturing; the paused time is not recorded
    record_keyboard: bool = True
    record_clicks: bool = True  # mouse buttons and the wheel


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
    def on_key_down(self, t: float, key: str, char: str | None = None) -> None:
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
        # the character actually typed keeps its case ("H"); the key name does not
        text_char = char or (" " if key == "space" else "+" if key == "plus" else key)
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


@dataclass
class RefineOptions:
    """Clean-ups offered after recording (see ``refine``)."""

    max_pause_ms: int = 0       # shorten longer pauses to this (0 = keep them)
    remove_pauses: bool = False
    remove_moves: bool = False
    join_lines: bool = False    # "text", Enter, "more text" -> one multi-line text


def refine(actions: list[Action], options: RefineOptions) -> list[Action]:
    """Return a cleaned-up copy of recorded ``actions`` (the originals are untouched)."""
    out = [a.clone() for a in actions]
    if options.remove_moves:
        out = [a for a in out if a.type != "mouse_move"]
    if options.remove_pauses:
        out = [a for a in out if a.type != "delay"]
    elif options.max_pause_ms > 0:
        for a in out:
            if a.type == "delay" and a.params.get("ms", 0) > options.max_pause_ms:
                a.params["ms"] = options.max_pause_ms
    if options.join_lines:
        joined: list[Action] = []
        for a in out:
            is_enter = a.type == "key_press" and a.params.get("keys") == "enter"
            if is_enter or a.type == "type_text":
                # the pauses between typing and Enter are part of typing the block
                pauses = 0
                while pauses < len(joined) and joined[-1 - pauses].type == "delay":
                    pauses += 1
                text = joined[-1 - pauses] if pauses < len(joined) else None
                if text is not None and text.type == "type_text":
                    del joined[len(joined) - pauses:]
                    text.params["text"] += "\n" if is_enter else a.params.get("text", "")
                    continue
            joined.append(a)
        out = joined
    return out


def build_actions(events: list[Event], options: RecordOptions | None = None) -> list[Action]:
    b = _Builder(options or RecordOptions())
    for t, kind, data in events:
        if kind == "key_down":
            if isinstance(data, tuple):
                b.on_key_down(t, *data)
            else:
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
        # physical ctrl / alt / cmd keys currently down -> canonical name. A set of the
        # keys themselves (not a counter) because held keys auto-repeat on some systems.
        self._held: dict[object, str] = {}
        # physical key -> name recorded at key down, reused for its key up so the
        # pair always matches (e.g. Option released before the letter on macOS)
        self._down_names: dict[object, str] = {}
        self._base_chars: dict[int, str] = {}
        self._stop_key_down = False
        self._pause_key_down = False
        self._paused_since: float | None = None
        self._paused_total = 0.0
        self._active = False
        self.on_pause_changed: Callable[[bool], None] | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def paused(self) -> bool:
        return self._paused_since is not None

    def toggle_pause(self) -> None:
        """Pause or resume capturing. The paused time is left out of the recording's
        clock, so resuming does not insert a long delay."""
        now = time.monotonic()
        with self._lock:
            if self._paused_since is None:
                self._paused_since = now
            else:
                self._paused_total += now - self._paused_since
                self._paused_since = None
        if self.on_pause_changed:
            self.on_pause_changed(self.paused)

    def start(self) -> None:
        from pynput import keyboard, mouse

        from keyboardmanager.core.backend import darwin_base_chars, safe_listener, start_listener

        if self._active:
            return
        self._events = []
        self._held, self._down_names = {}, {}
        self._paused_since, self._paused_total = None, 0.0
        self._base_chars = darwin_base_chars()  # on the calling (GUI) thread, see its docs
        self._active = True
        self._kb = safe_listener(keyboard.Listener)(on_press=self._press, on_release=self._release)
        self._mouse = safe_listener(mouse.Listener)(
            on_move=self._move, on_click=self._click, on_scroll=self._scroll
        )
        start_listener(self._kb)
        start_listener(self._mouse)

    def stop(self) -> list[Action]:
        """Stop listening and return the recorded actions."""
        from keyboardmanager.core.backend import stop_listener

        if self._active:
            self._active = False
            for listener in (self._kb, self._mouse):
                if listener is not None:
                    stop_listener(listener)
        with self._lock:
            events = list(self._events)
        return build_actions(events, self.options)

    # -- listener callbacks (run on pynput threads)
    def _add(self, kind: str, data: object) -> None:
        if not self._active:
            return
        if kind in ("key_down", "key_up") and not self.options.record_keyboard:
            return
        if kind in ("click", "scroll") and not self.options.record_clicks:
            return
        with self._lock:
            if self._paused_since is not None:
                return
            self._events.append((time.monotonic() - self._paused_total, kind, data))
            n = len(self._events)
        if self._on_event:
            self._on_event(n)

    def _name(self, key) -> str | None:
        from keyboardmanager.core.backend import pynput_key_to_name

        # With ctrl / alt / cmd held the key is a shortcut, so record the key itself
        # (macOS Option+S -> "alt+s", not "alt+ß"). Shift alone is typing: keep "S".
        return pynput_key_to_name(
            key, ctrl_held="ctrl" in self._held.values(),
            base_chars=self._base_chars if self._held else None,
        )

    @staticmethod
    def _ident(key) -> object:
        """Identity of the physical key: its key code when known, else the key."""
        vk = getattr(key, "vk", None)
        return ("vk", vk) if vk is not None else key

    def _press(self, key, injected: bool = False) -> None:
        if injected:  # sent by Keyboard Manager itself (a macro, a snippet, a remap)
            return
        name = self._name(key)
        if name is None:
            return
        self._down_names[self._ident(key)] = name
        if name == self.options.stop_key:
            if not self._stop_key_down:
                self._stop_key_down = True
                self._active_stop()
            return
        if name == self.options.pause_key:
            if not self._pause_key_down:
                self._pause_key_down = True
                self.toggle_pause()
            return
        if name in ("ctrl", "alt", "cmd"):
            self._held[self._ident(key)] = name
        char = getattr(key, "char", None)
        if not self._held and isinstance(char, str) and len(char) == 1 and char.isprintable():
            self._add("key_down", (name, char))  # typing: keep the exact character
        else:
            self._add("key_down", name)

    def _release(self, key, injected: bool = False) -> None:
        if injected:
            return
        name = self._down_names.pop(self._ident(key), None) or self._name(key)
        if name is None:
            return
        if name == self.options.stop_key:
            self._stop_key_down = False
            return
        if name == self.options.pause_key:
            self._pause_key_down = False
            return
        self._held.pop(self._ident(key), None)
        self._add("key_up", name)

    def _active_stop(self) -> None:
        if self._on_stopped:
            self._on_stopped()

    def _move(self, x, y, injected: bool = False) -> None:
        if not injected:
            self._add("move", (int(x), int(y)))

    def _click(self, x, y, button, pressed, injected: bool = False) -> None:
        if not injected:
            self._add("click", (int(x), int(y), button.name, bool(pressed)))

    def _scroll(self, x, y, dx, dy, injected: bool = False) -> None:
        if not injected:
            self._add("scroll", (int(x), int(y), int(dx), int(dy)))
