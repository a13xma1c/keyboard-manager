import pytest

from keyboardmanager.core import keys


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Ctrl+Shift+K", "ctrl+shift+k"),
        ("shift+ctrl+k", "ctrl+shift+k"),
        ("Win+E", "cmd+e"),
        ("command+Space", "cmd+space"),
        ("Return", "enter"),
        ("ctrl++", "ctrl+plus"),
        ("F5", "f5"),
        ("PgUp", "page_up"),
    ],
)
def test_normalize_combo(text, expected):
    assert keys.normalize_combo(text) == expected


@pytest.mark.parametrize("text", ["", "ctrl+", "ctrl+bogus", "+ +"])
def test_invalid_combo(text):
    with pytest.raises(ValueError):
        keys.normalize_combo(text)


def test_hotkey_needs_exactly_one_regular_key():
    assert keys.normalize_hotkey("alt+ctrl+f12") == "ctrl+alt+f12"
    for bad in ("ctrl+alt", "a+b", "ctrl+plus"):
        with pytest.raises(ValueError):
            keys.normalize_hotkey(bad)


def test_backend_names():
    assert keys.to_pynput_hotkey("ctrl+alt+k") == "<ctrl>+<alt>+k"
    assert keys.to_pynput_hotkey("cmd+f5") == "<cmd>+<f5>"
    assert keys.to_keyboard_hotkey("cmd+page_up") == "windows+page up"


def test_pynput_accepts_generated_hotkey_specs():
    from pynput import keyboard

    for hk in ("ctrl+alt+k", "cmd+f5", "shift+page_up", "ctrl+alt+f12", "alt+space"):
        keyboard.HotKey.parse(keys.to_pynput_hotkey(hk))
