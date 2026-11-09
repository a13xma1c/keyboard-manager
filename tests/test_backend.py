"""Input backend: key translation both ways and the pynput-based backend (with fake
controllers, so no real input is ever injected)."""

import pytest
from pynput.keyboard import Key, KeyCode

from keyboardmanager.core import backend as backend_mod
from keyboardmanager.core import keys
from keyboardmanager.core.backend import name_to_pynput_key, pynput_key_to_name
from keyboardmanager.core.models import ActiveWindow


@pytest.mark.parametrize("key,expected", [
    (Key.ctrl_l, "ctrl"), (Key.ctrl_r, "ctrl"), (Key.alt_gr, "alt"), (Key.shift_r, "shift"),
    (Key.cmd_r, "cmd"), (Key.page_up, "page_up"), (Key.f5, "f5"), (Key.enter, "enter"),
    (Key.esc, "esc"), (Key.space, "space"),
    (KeyCode.from_char("A"), "a"), (KeyCode.from_char(" "), "space"), (KeyCode.from_char("+"), "plus"),
    (KeyCode.from_char("!"), "!"), (KeyCode.from_char("é"), "é"),
    (KeyCode.from_vk(200), "vk:200"),
])
def test_pynput_key_to_name(key, expected):
    assert pynput_key_to_name(key) == expected


def test_pynput_key_to_name_edge_cases():
    # Windows reports ctrl+c as the control character \x03
    assert pynput_key_to_name(KeyCode.from_char("\x03"), ctrl_held=True) == "c"
    assert pynput_key_to_name(None) is None
    assert pynput_key_to_name(KeyCode()) is None
    media = getattr(Key, "media_play_pause", None)
    if media is not None:  # a Key with no canonical name
        assert pynput_key_to_name(media) is None


def test_name_to_pynput_key_round_trip():
    for name in ("ctrl", "alt", "shift", "cmd", "enter", "f12", "page_down", "space", "tab"):
        assert pynput_key_to_name(name_to_pynput_key(name)) == name
    assert name_to_pynput_key("a") == KeyCode.from_char("a")
    assert name_to_pynput_key("plus") == KeyCode.from_char("+")
    assert name_to_pynput_key("vk:65").vk == 65
    with pytest.raises(ValueError):
        name_to_pynput_key("bogus")


def test_every_named_key_has_a_pynput_key_or_a_clear_error():
    # Not every platform has every key (a Mac has no Insert); the error must say so
    for name in sorted(keys.NAMED_KEYS):
        try:
            name_to_pynput_key(name)
        except ValueError as exc:
            assert "not supported on this platform" in str(exc)


class _FakeKeyboard:
    def __init__(self):
        self.calls = []

    def press(self, k): self.calls.append(("press", k))
    def release(self, k): self.calls.append(("release", k))
    def type(self, text): self.calls.append(("type", text))


class _FakeMouse:
    def __init__(self):
        self.calls, self.position = [], (10.6, 20.2)

    def press(self, b): self.calls.append(("press", b))
    def release(self, b): self.calls.append(("release", b))
    def click(self, b, n): self.calls.append(("click", b, n))
    def scroll(self, dx, dy): self.calls.append(("scroll", dx, dy))


@pytest.fixture
def pynput_backend(monkeypatch):
    from pynput import keyboard, mouse

    monkeypatch.setattr(keyboard, "Controller", _FakeKeyboard)
    monkeypatch.setattr(mouse, "Controller", _FakeMouse)
    return backend_mod.PynputBackend()


def test_pynput_backend_keyboard(pynput_backend):
    b = pynput_backend
    b.key_down("ctrl")
    b.key_up("ctrl")
    b.key_down("a")
    b.type_text("Hi ✓")
    assert b._kb.calls == [("press", Key.ctrl), ("release", Key.ctrl), ("press", KeyCode.from_char("a")),
                           ("type", "Hi ✓")]


def test_pynput_backend_mouse(pynput_backend, monkeypatch):
    from pynput.mouse import Button

    b = pynput_backend
    assert b.mouse_position() == (10, 20)
    b.mouse_move(5, 6)
    assert b._mouse.position == (5, 6)
    b.mouse_down("left")
    b.mouse_up("right")
    b.mouse_click("middle", 2)
    b.mouse_scroll(0, -3)
    assert b._mouse.calls == [("press", Button.left), ("release", Button.right),
                              ("click", Button.middle, 2), ("scroll", 0, -3)]
    monkeypatch.setattr(backend_mod, "get_active_window", lambda: ActiveWindow("X", "Y"))
    assert b.active_window() == ActiveWindow("X", "Y")
