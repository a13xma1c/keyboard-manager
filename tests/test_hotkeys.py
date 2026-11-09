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
