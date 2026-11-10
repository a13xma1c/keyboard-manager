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


@pytest.mark.parametrize("text,expected", [
    ("Ctrl+K Ctrl+S", "ctrl+k ctrl+s"),
    ("  ctrl+alt+k  ", "ctrl+alt+k"),
    ("ctrl+k  shift+s  x", "ctrl+k shift+s x"),
])
def test_normalize_hotkey_sequence(text, expected):
    assert keys.normalize_hotkey_sequence(text) == expected
    assert keys.sequence_chords(text) == expected.split()


@pytest.mark.parametrize("text", ["", "ctrl+k ctrl", "a b c d", "ctrl+k bogus+x"])
def test_invalid_hotkey_sequences(text):
    with pytest.raises(ValueError):
        keys.normalize_hotkey_sequence(text)


@pytest.mark.parametrize("a,b,conflict", [
    ("ctrl+k", "ctrl+k", True),
    ("ctrl+k", "ctrl+k ctrl+s", True),          # pressing the prefix fires the short one
    ("ctrl+k ctrl+s", "ctrl+k", True),
    ("ctrl+k ctrl+s", "ctrl+k ctrl+s x", True),
    ("ctrl+k ctrl+a", "ctrl+k ctrl+s", False),  # shared first chord alone is fine
    ("ctrl+j", "ctrl+k ctrl+s", False),
])
def test_sequence_prefix_conflict(a, b, conflict):
    assert keys.sequence_prefix_conflict(a, b) is conflict
    assert keys.sequence_prefix_conflict(b, a) is conflict
