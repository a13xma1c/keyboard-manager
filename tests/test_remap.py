"""Key remapping. Nothing here remaps the real keyboard: hidutil and the keyboard
package are replaced by recorders (the native test only *reads* the macOS mapping)."""

import json
import sys
import types

import pytest

from keyboardmanager.core import keys
from keyboardmanager.core import remap as remap_mod
from keyboardmanager.core.remap import (
    HidutilRemapper,
    KeyboardPackageRemapper,
    Remap,
    hidutil_pairs,
    hidutil_set_argument,
    parse_hidutil_mapping,
    validate_remaps,
)


# ---------------------------------------------------------------- validation
def test_validation_normalises_names():
    assert validate_remaps([Remap("CapsLock", "Escape"), Remap("F13", "Win")]) == [
        Remap("caps_lock", "esc"), Remap("f13", "cmd")]


@pytest.mark.parametrize("remaps,problem", [
    ([Remap("a", "a")], "itself"),
    ([Remap("a", "b"), Remap("a", "c")], "more than once"),
    ([Remap("a", "b"), Remap("b", "a")], "swaps"),   # swap
    ([Remap("a", "b"), Remap("b", "c")], "chains"),  # chain
    ([Remap("nonsense", "a")], "unknown key"),
    ([Remap("plus", "a")], "cannot be remapped"),
])
def test_invalid_remaps(remaps, problem):
    with pytest.raises(ValueError, match=problem):
        validate_remaps(remaps)


def test_json_round_trip_and_errors():
    remaps = [Remap("caps_lock", "esc")]
    assert remap_mod.remaps_from_json(remap_mod.remaps_to_json(remaps)) == remaps
    assert remap_mod.remaps_from_json("") == []
    for bad in ('{"from": "a"}', '[{"from": "a"}]', '[{"from": "a", "to": "a"}]'):
        with pytest.raises(ValueError):
            remap_mod.remaps_from_json(bad)


# -------------------------------------------------------------------- macOS
def test_hid_table_covers_the_keyboard():
    usages = remap_mod.HID_USAGES
    assert set("abcdefghijklmnopqrstuvwxyz0123456789") <= set(usages)
    assert {"caps_lock", "esc", "enter", "ctrl", "shift", "alt", "cmd", "f1", "f24"} <= set(usages)
    assert len(set(usages.values())) == len(usages)  # no two names share a key
    assert usages["a"] == 0x04 and usages["caps_lock"] == 0x39 and usages["esc"] == 0x29


def test_hidutil_pairs_cover_both_sides_of_a_modifier():
    assert hidutil_pairs([Remap("caps_lock", "esc")]) == [(0x700000039, 0x700000029)]
    # a remapped modifier affects the left and the right key
    assert hidutil_pairs([Remap("ctrl", "cmd")]) == [(0x7000000E0, 0x7000000E3), (0x7000000E4, 0x7000000E3)]


def test_hidutil_argument_matches_apples_documented_format():
    # Apple TN2450: hidutil property --set '{"UserKeyMapping":[{"HIDKeyboardModifierMappingSrc":0x700000039,
    #                                                            "HIDKeyboardModifierMappingDst":0x700000002}]}'
    assert hidutil_set_argument([(0x700000039, 0x700000002)]) == (
        '{"UserKeyMapping":[{"HIDKeyboardModifierMappingSrc":0x700000039,'
        '"HIDKeyboardModifierMappingDst":0x700000002}]}')
    assert hidutil_set_argument([]) == '{"UserKeyMapping":[]}'


GET_OUTPUT = """(
        {
        HIDKeyboardModifierMappingDst = 30064771113;
        HIDKeyboardModifierMappingSrc = 30064771129;
    },
        {
        HIDKeyboardModifierMappingSrc = 30064771300;
        HIDKeyboardModifierMappingDst = 30064771299;
    }
)
"""


def test_parse_hidutil_mapping():
    assert parse_hidutil_mapping("(null)\n") == []
    assert parse_hidutil_mapping(GET_OUTPUT) == [(30064771129, 30064771113), (30064771300, 30064771299)]


class Hidutil:
    """Records hidutil calls; ``current`` is what --get reports."""

    def __init__(self, current="(null)"):
        self.current, self.commands, self.fail = current, [], False

    def __call__(self, cmd):
        if self.fail:
            raise OSError("hidutil: not permitted")
        self.commands.append(cmd)
        if cmd[:3] == ["hidutil", "property", "--get"]:
            return self.current
        assert cmd[:3] == ["hidutil", "property", "--set"]
        self.current = cmd[3]
        return ""

    def mapping(self):
        """The pairs of the last --set."""
        text = self.commands[-1][3]
        return [tuple(int(v, 16) for v in pair) for pair in
                __import__("re").findall(r"Src\":(0x[0-9a-f]+),\"HIDKeyboardModifierMappingDst\":(0x[0-9a-f]+)", text)]


@pytest.fixture
def settings():
    store = {}
    return store, (lambda key, default="": store.get(key, default)), store.__setitem__


def test_hidutil_keeps_and_restores_another_tools_mapping(settings):
    store, get, set_ = settings
    tool = Hidutil(GET_OUTPUT)
    r = HidutilRemapper(get, set_, run=tool)
    assert r.apply([Remap("caps_lock", "esc")]) == {}
    # the foreign pair for caps_lock is replaced by ours; the other one is kept
    assert tool.mapping() == [(30064771300, 30064771299), (0x700000039, 0x700000029)]
    assert json.loads(store[remap_mod.PREVIOUS_SETTING]) == [[30064771129, 30064771113], [30064771300, 30064771299]]
    r.apply([Remap("f13", "esc")])  # changes do not overwrite the saved original
    assert len(json.loads(store[remap_mod.PREVIOUS_SETTING])) == 2
    r.clear()
    assert tool.mapping() == [(30064771129, 30064771113), (30064771300, 30064771299)]
    assert store[remap_mod.PREVIOUS_SETTING] == ""


def test_hidutil_without_a_previous_mapping(settings):
    store, get, set_ = settings
    tool = Hidutil()
    r = HidutilRemapper(get, set_, run=tool)
    r.apply([Remap("caps_lock", "esc")])
    assert store[remap_mod.PREVIOUS_SETTING] == "[]"
    r.clear()
    assert tool.commands[-1][3] == '{"UserKeyMapping":[]}'
    calls = len(tool.commands)
    r.clear()  # nothing active: no hidutil call at all
    assert len(tool.commands) == calls


def test_hidutil_crash_recovery(settings):
    """If Keyboard Manager died with remaps active, the next start restores the mapping."""
    store, get, set_ = settings
    store[remap_mod.PREVIOUS_SETTING] = json.dumps([])
    tool = Hidutil(current="...our stale mapping...")
    HidutilRemapper(get, set_, run=tool).recover()
    assert tool.commands == [["hidutil", "property", "--set", '{"UserKeyMapping":[]}']]
    assert store[remap_mod.PREVIOUS_SETTING] == ""


def test_hidutil_failures_are_reported_and_recoverable(settings):
    store, get, set_ = settings
    tool = Hidutil()
    r = HidutilRemapper(get, set_, run=tool)
    tool.fail = True
    assert "hidutil failed" in r.apply([Remap("caps_lock", "esc")])["*"]
    tool.fail = False
    r.apply([Remap("caps_lock", "esc")])
    tool.fail = True
    r.clear()  # could not undo: keep the saved mapping so the next start restores it
    assert store[remap_mod.PREVIOUS_SETTING] == "[]"


def test_hidutil_rejects_keys_it_cannot_address(settings):
    _, get, set_ = settings
    r = HidutilRemapper(get, set_, run=Hidutil())
    assert "cannot be remapped on macOS" in r.apply([Remap("é", "a")])["*"]


# ---------------------------------------------------------- Windows / Linux
class FakeKeyboardPackage(types.ModuleType):
    def __init__(self, reject=()):
        super().__init__("keyboard")
        self.remapped, self.unremapped, self.reject = [], [], set(reject)

    def remap_key(self, src, dst):
        if src in self.reject:
            raise ValueError(f"{src} is not supported")
        self.remapped.append((src, dst))

    def unremap_key(self, src):
        self.unremapped.append(src)


def test_keyboard_package_remapper(monkeypatch):
    package = FakeKeyboardPackage(reject={"print screen"})
    monkeypatch.setitem(sys.modules, "keyboard", package)
    r = KeyboardPackageRemapper()
    failures = r.apply([Remap("caps_lock", "esc"), Remap("cmd", "alt"), Remap("print_screen", "f1")])
    assert package.remapped == [("caps lock", "esc"), ("windows", "alt")]  # the package's names
    assert list(failures) == ["print_screen"]
    r.clear()
    assert package.unremapped == ["caps lock", "windows"]
    r.clear()  # idempotent
    assert len(package.unremapped) == 2


def test_linux_without_root_is_unsupported_with_a_reason(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(remap_mod.os, "geteuid", lambda: 1000, raising=False)
    backend = remap_mod.create_remap_backend(lambda k, d="": d, lambda k, v: None)
    assert isinstance(backend, remap_mod.UnsupportedRemapper)
    assert "root" in backend.unavailable_reason()
    assert backend.apply([Remap("a", "b")]) == {"*": backend.unavailable_reason()}
    assert backend.apply([]) == {}


def test_backend_selection(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyboard", FakeKeyboardPackage())
    assert isinstance(remap_mod.create_remap_backend(None, None, platform="darwin"), HidutilRemapper)
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(remap_mod.create_remap_backend(None, None, platform="win32"), KeyboardPackageRemapper)
    monkeypatch.setitem(sys.modules, "keyboard", None)  # package missing
    backend = remap_mod.create_remap_backend(None, None, platform="win32")
    assert isinstance(backend, remap_mod.UnsupportedRemapper) and "not available" in backend.unavailable_reason()


@pytest.mark.native
@pytest.mark.skipif(sys.platform != "darwin", reason="needs a real Mac")
def test_native_hidutil_mapping_can_be_read():
    """Read-only: the current mapping is parsed without changing anything."""
    output = remap_mod._run(["hidutil", "property", "--get", "UserKeyMapping"])
    assert isinstance(parse_hidutil_mapping(output), list)


def test_every_hid_name_is_a_valid_key_name():
    for name in remap_mod.HID_USAGES:
        assert keys.normalize_key(name) == name
