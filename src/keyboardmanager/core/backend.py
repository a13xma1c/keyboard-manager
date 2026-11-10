"""Input backends: the only place that talks to the operating system for synthetic input.

``InputBackend`` is a small protocol so the executor can be tested with a fake.
``PynputBackend`` is the real implementation; pynput is imported lazily so that
importing this module never requires a display server.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Protocol

from keyboardmanager.core import keys
from keyboardmanager.core.models import ActiveWindow
from keyboardmanager.core.windowinfo import get_active_window


class InputBackend(Protocol):
    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def type_text(self, text: str) -> None: ...
    def mouse_position(self) -> tuple[int, int]: ...
    def mouse_move(self, x: int, y: int) -> None: ...
    def mouse_down(self, button: str) -> None: ...
    def mouse_up(self, button: str) -> None: ...
    def mouse_click(self, button: str, count: int) -> None: ...
    def mouse_scroll(self, dx: int, dy: int) -> None: ...
    def active_window(self) -> ActiveWindow: ...
    def launch(self, target: str) -> None: ...


# ----------------------------------------------------------------- key mapping
_MODIFIER_NAMES = {
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd",
}


# macOS text-input APIs (TISCopyCurrentKeyboardInputSource & co.) must only be called
# on the main thread: recent macOS versions terminate the process otherwise.
_last_base_chars: dict[int, str] = {}


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _main_thread_keycode_context() -> Any:
    """pynput's keyboard-layout context, computed here on the main thread (or ``None``)."""
    if not _on_main_thread():
        return None
    try:
        from pynput._util.darwin import keycode_context

        with keycode_context() as context:
            return context  # plain data (keyboard type, layout bytes): safe to share
    except Exception:  # noqa: BLE001
        return None


def darwin_base_chars() -> dict[int, str]:
    """macOS: key code -> character the key types *without* modifiers, for the
    current keyboard layout. ``{}`` on other platforms or if unavailable.

    On macOS pynput reports the composed character, so Option+S arrives as ``ß``
    and Option+K as ``˚``; this table maps such keys back to ``s`` / ``k``. Reading
    the layout uses main-thread-only macOS APIs: off the main thread the table from
    the previous call is returned instead.
    """
    global _last_base_chars
    if sys.platform != "darwin":
        return {}
    if not _on_main_thread():
        return dict(_last_base_chars)
    try:
        from pynput._util.darwin import keycode_context, keycode_to_string

        table: dict[int, str] = {}
        with keycode_context() as context:
            for vk in range(128):
                char = keycode_to_string(context, vk)
                if len(char) == 1 and char.isprintable() and not char.isspace():
                    table[vk] = char.lower()
        _last_base_chars = table
        return dict(table)
    except Exception:  # noqa: BLE001 - fall back to pynput's characters
        return {}


def safe_listener(cls: type) -> type:
    """Subclass of a pynput listener class that avoids two pynput pitfalls.

    * ``stop()`` is ignored by pynput until the listener thread has started running,
      so a listener stopped right after ``start()`` would keep running (and keep
      its hotkeys active) forever. Here a stop request is remembered and honoured
      when the thread starts.
    * macOS: pynput's keyboard listener reads the keyboard layout (TIS APIs) at the
      start of its *own thread*; recent macOS versions kill the process for that. The
      value (``_context``) is never used, so it is computed on the main thread when
      the listener is created and the thread starts its event loop without the call.
    * macOS: stopping could call ``CFRunLoopStop(NULL)`` (a check-then-use race in
      pynput) and crash; the run loop is read once here.
    * Linux: stopping a listener that failed to start waits forever in pynput; here
      the wait is bounded, and errors raised while a listener is torn down (the X
      connection closing) are not reported as crashes.

    Use it together with ``start_listener`` / ``stop_listener``.
    """
    mixin = None
    if sys.platform == "darwin":
        try:
            from pynput._util.darwin import ListenerMixin

            mixin = ListenerMixin if ListenerMixin in cls.__mro__ else None
        except Exception:  # noqa: BLE001
            mixin = None

    class SafeListener(cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._stop_requested = False
            if mixin is not None:
                self._main_thread_context = _main_thread_keycode_context()

        def stop(self):
            self._stop_requested = True
            super().stop()

        def run(self):
            try:
                super().run()
            except Exception:
                # Errors while being torn down (X11: "display connection closed") are
                # noise from a listener that is being discarded; anything else is real.
                if not self._stop_requested:
                    raise

        def _stop_platform(self):
            if mixin is None:
                # pynput's X11 backend waits *without a timeout* for a listener that
                # never became ready (e.g. its X connection failed), which would freeze
                # the GUI thread. Give it a moment; if it never came up, there is nothing
                # to stop (and stale hotkey callbacks are silenced by PynputHotkeys).
                if not getattr(self, "_ready", True):
                    condition = getattr(self, "_condition", None)
                    if condition is not None:
                        with condition:
                            condition.wait_for(lambda: self._ready or not self.is_alive(), 2.0)
                    if not getattr(self, "_ready", True):
                        return
                super()._stop_platform()
                return
            # pynput reads self._loop twice ("if self._loop is not None:
            # CFRunLoopStop(self._loop)") while the listener thread may set it to None
            # on exit, which ends in CFRunLoopStop(NULL): a segmentation fault.
            loop = getattr(self, "_loop", None)
            if loop is not None:
                from Quartz import CFRunLoopStop

                CFRunLoopStop(loop)

        def _run(self):
            if self._stop_requested:  # stopped before the thread got going
                return
            if mixin is None:
                super()._run()
                return
            self._context = self._main_thread_context
            try:
                mixin._run(self)  # the event tap loop, minus pynput's TIS call
            finally:
                self._context = None

    SafeListener.__name__ = f"Safe{cls.__name__}"
    return SafeListener


def start_listener(listener: Any, timeout: float = 2.0) -> None:
    """Start a pynput listener and wait (bounded) until it is ready.

    Stopping a pynput listener while it is still setting up races with that setup
    (on Linux ``stop()`` raises, on macOS it touches a run loop that may not exist
    yet). Waiting for readiness, as pynput recommends, closes that window; the
    timeout keeps the GUI responsive if a listener fails to start.
    """
    listener.start()
    condition = getattr(listener, "_condition", None)
    if condition is None:
        return
    with condition:
        condition.wait_for(lambda: getattr(listener, "_ready", True) or not listener.is_alive(), timeout)


def stop_listener(listener: Any) -> None:
    """Stop a pynput listener; a listener that fails to stop cleanly is abandoned."""
    try:
        listener.stop()
    except Exception:  # noqa: BLE001 - e.g. Xlib: display connection already closed
        pass


def base_key(key: Any, base_chars: dict[int, str]) -> Any:
    """Replace a character key by its unmodified character (see ``darwin_base_chars``)."""
    vk = getattr(key, "vk", None)
    if getattr(key, "char", None) is None or vk not in base_chars:
        return key
    from pynput.keyboard import KeyCode

    return KeyCode.from_char(base_chars[vk], vk=vk)


def pynput_key_to_name(
    key: Any, ctrl_held: bool = False, base_chars: dict[int, str] | None = None
) -> str | None:
    """Translate a key object received from a pynput listener to a canonical name.

    Returns ``None`` for keys we cannot represent. When ``ctrl_held`` is true a
    control character (what Windows reports for ctrl+letter) is mapped back to
    its letter. ``base_chars`` (pass it while ctrl / alt / cmd are held) maps keys
    to their unmodified character, so macOS Option+S is ``s`` rather than ``ß``.
    """
    if base_chars:
        key = base_key(key, base_chars)
    name = getattr(key, "name", None)
    if name is not None:  # pynput.keyboard.Key member
        name = _MODIFIER_NAMES.get(name, name)
        try:
            return keys.normalize_key(name)
        except ValueError:
            return None
    char = getattr(key, "char", None)
    if char:
        if not char.isprintable() and len(char) == 1 and 1 <= ord(char) <= 26:
            char = chr(ord(char) + 96)
        if char.isprintable():
            return {" ": "space", "+": "plus"}.get(char, char.lower())
    vk = getattr(key, "vk", None)
    if vk is not None:
        return f"vk:{vk}"
    return None


def name_to_pynput_key(name: str) -> Any:
    """Canonical key name -> object accepted by ``pynput.keyboard.Controller``."""
    from pynput.keyboard import Key, KeyCode

    name = keys.normalize_key(name)
    if name == "plus":
        return KeyCode.from_char("+")
    if name.startswith("vk:"):
        return KeyCode.from_vk(int(name[3:]))
    member = getattr(Key, name, None)
    if member is not None:
        return member
    if len(name) == 1:
        return KeyCode.from_char(name)
    raise ValueError(f"key {name!r} is not supported on this platform")


def key_available(name: str) -> bool:
    """Whether this system can send the key (a Mac has no Insert key, for instance).
    Unknown (pynput cannot be loaded) counts as available."""
    try:
        name_to_pynput_key(name)
    except ValueError:
        return False
    except Exception:  # noqa: BLE001 - e.g. no display for pynput on Linux
        return True
    return True


class PynputBackend:
    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._kb = keyboard.Controller()
        self._mouse = mouse.Controller()
        self._buttons = {
            "left": mouse.Button.left,
            "right": mouse.Button.right,
            "middle": mouse.Button.middle,
        }

    def key_down(self, key: str) -> None:
        self._kb.press(name_to_pynput_key(key))

    def key_up(self, key: str) -> None:
        self._kb.release(name_to_pynput_key(key))

    def type_text(self, text: str) -> None:
        self._kb.type(text)

    def mouse_position(self) -> tuple[int, int]:
        x, y = self._mouse.position
        return int(x), int(y)

    def mouse_move(self, x: int, y: int) -> None:
        self._mouse.position = (x, y)

    def mouse_down(self, button: str) -> None:
        self._mouse.press(self._buttons[button])

    def mouse_up(self, button: str) -> None:
        self._mouse.release(self._buttons[button])

    def mouse_click(self, button: str, count: int) -> None:
        self._mouse.click(self._buttons[button], count)

    def mouse_scroll(self, dx: int, dy: int) -> None:
        self._mouse.scroll(dx, dy)

    def active_window(self) -> ActiveWindow:
        return get_active_window()

    def launch(self, target: str) -> None:
        from keyboardmanager.core.launcher import launch

        launch(target)
