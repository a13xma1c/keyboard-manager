"""Global hotkey backends (no real hooks are installed: pynput and `keyboard` are faked)."""

import sys

import pytest

from keyboardmanager.core import hotkeys


class _FakeKeyboardModule:
    """Stand-in for the `keyboard` package."""

    def __init__(self, reject=()):
        self.registered, self.removed, self.reject = {}, [], set(reject)

    def add_hotkey(self, spec, cb):
        if spec in self.reject or "*" in self.reject:
            raise ValueError(f"cannot hook {spec}")
        handle = f"h{len(self.registered)}"
        self.registered[handle] = (spec, cb)
        return handle

    def remove_hotkey(self, handle):
        if handle == "gone":
            raise KeyError(handle)
        self.removed.append(handle)


@pytest.fixture
def keyboard_pkg(monkeypatch):
    fake = _FakeKeyboardModule()
    monkeypatch.setitem(sys.modules, "keyboard", fake)
    return fake


def test_keyboard_backend_registers_and_translates(keyboard_pkg):
    hk = hotkeys.KeyboardHotkeys()
    fired = []
    failures = hk.start({"cmd+page_up": lambda: fired.append(1), "ctrl+alt+f12": lambda: None})
    assert failures == {}
    specs = sorted(spec for spec, _ in keyboard_pkg.registered.values())
    assert specs == ["ctrl+alt+f12", "windows+page up"]
    keyboard_pkg.registered["h0"][1]()
    assert fired == [1]
    hk.stop()
    assert sorted(keyboard_pkg.removed) == ["h0", "h1"]
    hk.stop()  # idempotent


def test_keyboard_backend_reports_failures(keyboard_pkg):
    keyboard_pkg.reject = {"ctrl+q"}
    hk = hotkeys.KeyboardHotkeys()
    assert list(hk.start({"ctrl+q": lambda: None, "ctrl+w": lambda: None})) == ["ctrl+q"]
    hk._handles.append("gone")  # already removed by the library: tolerated
    hk.stop()


def test_fallback_uses_keyboard_package_when_it_works(keyboard_pkg, monkeypatch):
    fb = hotkeys.FallbackHotkeys()
    monkeypatch.setattr(fb._secondary, "start", lambda m: pytest.fail("pynput must not be used"))
    assert fb.start({"ctrl+1": lambda: None}) == {} and fb.name == "keyboard"


def test_fallback_switches_to_pynput_when_keyboard_cannot_hook(keyboard_pkg, monkeypatch):
    keyboard_pkg.reject = {"*"}  # e.g. Linux without root
    fb = hotkeys.FallbackHotkeys()
    used = {}
    monkeypatch.setattr(fb._secondary, "start", lambda m: used.setdefault("mapping", m) and {})
    fb.start({"ctrl+1": lambda: None})
    assert fb.name == "pynput" and set(used["mapping"]) == {"ctrl+1"}


def test_fallback_switches_when_keyboard_package_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyboard", None)
    fb = hotkeys.FallbackHotkeys()
    monkeypatch.setattr(fb._secondary, "start", lambda m: {})
    fb.start({"ctrl+1": lambda: None})
    assert fb.name == "pynput"
    stopped = []
    monkeypatch.setattr(fb._primary, "stop", lambda: stopped.append("k"))
    monkeypatch.setattr(fb._secondary, "stop", lambda: stopped.append("p"))
    fb.stop()
    assert stopped == ["k", "p"]


def test_pynput_backend_without_valid_hotkeys_starts_nothing(monkeypatch):
    monkeypatch.setattr(hotkeys, "layout_aware_hotkeys_class", lambda: pytest.fail("no listener expected"))
    hk = hotkeys.PynputHotkeys()
    assert list(hk.start({"ctrl+nonsense": lambda: None})) == ["ctrl+nonsense"]
    assert hk._listener is None
    hk.stop()


@pytest.mark.parametrize("platform,kind,expected", [
    ("linux", "auto", hotkeys.PynputHotkeys),
    ("linux", "keyboard", hotkeys.KeyboardHotkeys),
    ("win32", "auto", hotkeys.FallbackHotkeys),
    ("win32", "pynput", hotkeys.PynputHotkeys),
    ("darwin", "keyboard", hotkeys.PynputHotkeys),
])
def test_backend_selection(monkeypatch, platform, kind, expected):
    monkeypatch.setattr(sys, "platform", platform)
    assert type(hotkeys.create_hotkey_backend(kind)) is expected


# ------------------------------------------------- pynput listener lifecycle
import threading  # noqa: E402
import time  # noqa: E402

from keyboardmanager.core import backend as backend_mod  # noqa: E402


def test_a_replaced_listener_can_never_fire(monkeypatch):
    """pynput does not always stop a listener (seen on X11); old callbacks must be inert."""
    listeners = []

    class Recording:
        def __init__(self, specs, base):
            self.specs = specs
            listeners.append(self)

        def start(self): pass
        def stop(self): pass  # pretend pynput failed to stop it

    monkeypatch.setattr(hotkeys, "layout_aware_hotkeys_class", lambda: Recording)
    fired = []
    hk = hotkeys.PynputHotkeys()
    hk.start({"ctrl+1": lambda: fired.append("first")})
    hk.start({"ctrl+1": lambda: fired.append("second")})
    listeners[0].specs["<ctrl>+1"]()  # the stale listener still sees the key...
    assert fired == []  # ...but does nothing
    listeners[1].specs["<ctrl>+1"]()
    assert fired == ["second"]
    hk.stop()  # e.g. global hotkeys switched off
    listeners[1].specs["<ctrl>+1"]()
    assert fired == ["second"]


class _FakePynputListener(threading.Thread):
    """Mimics pynput's AbstractListener: stop() is ignored until run() has begun."""

    def __init__(self, ready_after=0.0, never_ready=False):
        super().__init__(daemon=True)
        self._running, self._ready = False, False
        self._condition = threading.Condition()
        self.ready_after, self.never_ready, self.loops = ready_after, never_ready, 0
        self._quit = threading.Event()

    def run(self):
        self._running = True
        self._run()

    def _run(self):
        time.sleep(self.ready_after)
        if not self.never_ready:
            with self._condition:
                self._ready = True
                self._condition.notify_all()
        while self._running and not self._quit.wait(0.01):
            self.loops += 1

    def stop(self):
        if self._running:  # pynput's behaviour: ignored before run() has begun
            self._running = False
            self._quit.set()

    def force_quit(self):
        self._quit.set()


def test_start_listener_waits_until_ready():
    listener = _FakePynputListener(ready_after=0.2)
    t = time.monotonic()
    backend_mod.start_listener(listener)
    assert listener._ready and time.monotonic() - t >= 0.19
    listener.stop()


def test_start_listener_gives_up_on_a_listener_that_never_becomes_ready():
    listener = _FakePynputListener(never_ready=True)
    t = time.monotonic()
    backend_mod.start_listener(listener, timeout=0.2)
    assert time.monotonic() - t < 1.0  # the GUI thread is not blocked for long
    listener.stop()


def test_a_listener_stopped_before_its_thread_runs_does_not_start():
    safe = backend_mod.safe_listener(_FakePynputListener)
    listener = safe()
    listener.stop()  # pynput would ignore this: run() has not begun yet
    listener.start()
    listener.join(0.5)
    alive = listener.is_alive()
    listener.force_quit()
    assert not alive and listener.loops == 0


def test_stop_listener_survives_a_failing_stop():
    class Broken:
        def stop(self):
            raise ConnectionError("Display connection closed by client")

    backend_mod.stop_listener(Broken())  # must not raise into the GUI


class _XorgLikeListener:
    """Mimics pynput's X11 listener: _stop_platform waits *forever* for readiness."""

    def __init__(self):
        self._ready, self._running = False, True
        self._condition = threading.Condition()
        self.platform_stopped = False

    def is_alive(self):
        return False  # its thread died while starting (e.g. the X connection failed)

    def stop(self):
        self._running = False
        self._stop_platform()

    def _stop_platform(self):
        with self._condition:
            while not self._ready:  # pynput: "if not hasattr(self, '_context'): self.wait()"
                self._condition.wait()
        self.platform_stopped = True


def test_stopping_a_listener_that_never_started_does_not_hang():
    listener = backend_mod.safe_listener(_XorgLikeListener)()
    stopper = threading.Thread(target=listener.stop, daemon=True)
    stopper.start()
    stopper.join(5)
    assert not stopper.is_alive(), "stop() hung (would freeze the GUI)"
    assert not listener.platform_stopped  # nothing was running, so nothing to stop


def test_a_listener_that_fails_to_start_is_retried_then_reported(monkeypatch):
    created = []
    outcomes = []

    class Flaky:
        def __init__(self, specs, base):
            self._ready = outcomes.pop(0)
            self.stopped = False
            created.append(self)

        def start(self): pass
        def stop(self): self.stopped = True

    monkeypatch.setattr(hotkeys, "layout_aware_hotkeys_class", lambda: Flaky)
    hk = hotkeys.PynputHotkeys()
    outcomes[:] = [False, True]  # first attempt fails, the retry works
    assert hk.start({"ctrl+1": lambda: None}) == {}
    assert len(created) == 2 and created[0].stopped and hk._listener is created[1]

    created.clear()
    outcomes[:] = [False, False]  # both fail: say so instead of silently having no hotkeys
    failures = hk.start({"ctrl+1": lambda: None})
    assert failures == {"*": "the global hotkey listener could not be started"}
