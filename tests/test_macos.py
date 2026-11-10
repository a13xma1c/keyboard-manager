"""macOS support.

Most tests simulate macOS (``sys.platform == "darwin"`` plus fake AppKit / Quartz /
HIServices modules) so the Mac code paths are verified on every OS. Tests marked
``native`` only run on a real Mac (e.g. the macOS CI job) and use the real APIs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pynput.keyboard import Key, KeyCode

from keyboardmanager.core import backend as backend_mod
from keyboardmanager.core import hotkeys, permissions, windowinfo
from keyboardmanager.core.models import ActiveWindow
from keyboardmanager.core.recorder import Recorder, RecordOptions

on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="needs a real Mac")
native = pytest.mark.native

# US layout key codes: 1 = S, 40 = K, 18 = 1
OPTION_S, OPTION_K = KeyCode(char="ß", vk=1), KeyCode(char="˚", vk=40)
US_BASE = {1: "s", 40: "k", 18: "1", 0: "a"}


# ------------------------------------------------------------- active window
class _App:
    def __init__(self, name, pid):
        self._name, self._pid = name, pid

    def localizedName(self):  # noqa: N802 - AppKit API
        return self._name

    def processIdentifier(self):  # noqa: N802 - AppKit API
        return self._pid


def _appkit(front):
    class Workspace:
        def frontmostApplication(self):  # noqa: N802
            return front

    return {"NSWorkspace": type("NSWorkspace", (), {"sharedWorkspace": staticmethod(Workspace)})}


def _quartz(windows, fail=False):
    def copy(options, window_id):
        if fail:
            raise RuntimeError("no window server")
        return windows

    return {
        "kCGWindowListOptionOnScreenOnly": 1, "kCGWindowListExcludeDesktopElements": 16,
        "kCGNullWindowID": 0, "CGWindowListCopyWindowInfo": copy,
    }


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def test_active_window_app_and_title(mac, fake_modules):
    fake_modules(
        AppKit=_appkit(_App("Safari", 42)),
        Quartz=_quartz([
            {"kCGWindowOwnerPID": 7, "kCGWindowLayer": 0, "kCGWindowName": "Other app"},
            {"kCGWindowOwnerPID": 42, "kCGWindowLayer": 25, "kCGWindowName": "Menu bar item"},
            {"kCGWindowOwnerPID": 42, "kCGWindowLayer": 0, "kCGWindowName": ""},
            {"kCGWindowOwnerPID": 42, "kCGWindowLayer": 0, "kCGWindowName": "Apple - Start"},
        ]),
    )
    assert windowinfo.get_active_window() == ActiveWindow("Safari", "Apple - Start")


def test_active_window_without_screen_recording_permission(mac, fake_modules):
    # macOS hides window titles without Screen Recording; the app name still works
    fake_modules(AppKit=_appkit(_App("Mail", 5)),
                 Quartz=_quartz([{"kCGWindowOwnerPID": 5, "kCGWindowLayer": 0}]))
    assert windowinfo.get_active_window() == ActiveWindow("Mail", "")


def test_active_window_degrades_gracefully(mac, fake_modules, monkeypatch):
    fake_modules(AppKit=_appkit(_App("Notes", 1)), Quartz=_quartz([], fail=True))
    assert windowinfo.get_active_window() == ActiveWindow("Notes", "")
    fake_modules(AppKit=_appkit(None))
    assert windowinfo.get_active_window() == ActiveWindow()
    monkeypatch.setitem(sys.modules, "AppKit", None)  # pyobjc missing entirely
    assert windowinfo.get_active_window() == ActiveWindow()


def test_app_specific_macro_matches_mac_app_names(mac, fake_modules):
    from keyboardmanager.core.models import Macro

    fake_modules(AppKit=_appkit(_App("Google Chrome", 3)), Quartz=_quartz([]))
    assert Macro("m", app_filter="chrome").matches(windowinfo.get_active_window())


# -------------------------------------------------------------- permissions
def test_permissions_all_granted(mac, fake_modules):
    fake_modules(HIServices={"AXIsProcessTrusted": lambda: True},
                 Quartz={"CGPreflightListenEventAccess": lambda: True})
    assert permissions.missing_permissions() == []


def test_permissions_missing(mac, fake_modules):
    fake_modules(HIServices={"AXIsProcessTrusted": lambda: False},
                 Quartz={"CGPreflightListenEventAccess": lambda: False})
    missing = permissions.missing_permissions()
    assert missing == [permissions.ACCESSIBILITY, permissions.INPUT_MONITORING]
    text = permissions.explain(missing)
    assert "Privacy & Security > Accessibility" in text and "Input Monitoring" in text


def test_permissions_unknown_counts_as_granted(mac, fake_modules, monkeypatch):
    fake_modules(HIServices={"AXIsProcessTrusted": lambda: True}, Quartz={})  # macOS < 10.15
    assert permissions.missing_permissions() == []
    monkeypatch.setitem(sys.modules, "HIServices", None)
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert permissions.missing_permissions() == []


def test_permissions_not_checked_elsewhere():
    assert permissions.missing_permissions("win32") == []
    assert permissions.missing_permissions("linux") == []


def test_main_window_warns_about_missing_permissions(qapp, db, backend, monkeypatch, no_modal_boxes):
    from keyboardmanager.core.service import MacroService
    from keyboardmanager.gui.mainwindow import MainWindow

    monkeypatch.setattr(permissions, "missing_permissions", lambda: [permissions.ACCESSIBILITY])
    svc = MacroService(db, backend, None, scheduler_thread=False)
    win = MainWindow(db, svc)
    qapp.processEvents()
    assert win.permission_warning.text() == "Permissions missing"
    assert "Accessibility" in win.permission_warning.toolTip()
    assert [kind for kind, _ in no_modal_boxes] == ["warning"]
    svc.shutdown()


# ----------------------------------------------------- Option-key handling
def test_darwin_base_chars(mac, fake_modules):
    from contextlib import contextmanager

    layout = {0: "a", 1: "s", 18: "1", 49: " ", 36: "\r", 50: "`", 10: ""}

    @contextmanager
    def context():
        yield "ctx"

    fake_modules(**{"pynput._util.darwin": {
        "keycode_context": context,
        "keycode_to_string": lambda ctx, vk: layout.get(vk, ""),
    }})
    assert backend_mod.darwin_base_chars() == {0: "a", 1: "s", 18: "1", 50: "`"}


def test_darwin_base_chars_failures(mac, fake_modules, monkeypatch):
    def broken():
        raise RuntimeError("TIS unavailable")

    fake_modules(**{"pynput._util.darwin": {"keycode_context": broken, "keycode_to_string": None}})
    assert backend_mod.darwin_base_chars() == {}
    monkeypatch.setattr(sys, "platform", "linux")
    assert backend_mod.darwin_base_chars() == {}


def test_base_key():
    assert backend_mod.base_key(OPTION_S, US_BASE) == KeyCode.from_char("s", vk=1)
    assert backend_mod.base_key(OPTION_S, {}) is OPTION_S
    assert backend_mod.base_key(Key.f12, US_BASE) is Key.f12
    assert backend_mod.base_key(KeyCode.from_vk(99), US_BASE).vk == 99


def _press_all(listener, keys_, injected=False):
    for key in keys_:
        listener._on_press(key, injected)


def test_option_hotkeys_fire_with_layout_aware_listener():
    from pynput import keyboard

    fired = []
    specs = {"<ctrl>+<alt>+s": lambda: fired.append("ctrl+alt+s"),
             "<alt>+k": lambda: fired.append("alt+k"),
             "<ctrl>+<shift>+1": lambda: fired.append("ctrl+shift+1")}
    # pynput's stock listener compares the composed character: never fires on a Mac
    stock = keyboard.GlobalHotKeys(specs)
    _press_all(stock, [Key.ctrl_l, Key.alt_l, OPTION_S])
    assert fired == []

    cls = hotkeys.layout_aware_hotkeys_class()
    for combo, expected in (([Key.ctrl_l, Key.alt_l, OPTION_S], "ctrl+alt+s"),
                            ([Key.alt_r, OPTION_K], "alt+k"),
                            ([Key.ctrl_l, Key.shift, KeyCode(char="!", vk=18)], "ctrl+shift+1")):
        listener = cls(specs, US_BASE)
        _press_all(listener, combo)
        assert fired[-1] == expected
    # events injected by a running macro never trigger hotkeys
    listener = cls(specs, US_BASE)
    _press_all(listener, [Key.ctrl_l, Key.alt_l, OPTION_S], injected=True)
    assert len(fired) == 3


def test_pynput_hotkeys_registers_layout_aware_listener(monkeypatch):
    created = {}

    class FakeListener:
        def __init__(self, specs, base_chars):
            created.update(specs=specs, base=base_chars)
            self.daemon = False

        def start(self):
            created["started"] = self.daemon

        def stop(self):
            created["stopped"] = True


    monkeypatch.setattr(hotkeys, "layout_aware_hotkeys_class", lambda: FakeListener)
    monkeypatch.setattr(backend_mod, "darwin_base_chars", lambda: US_BASE)
    hk = hotkeys.PynputHotkeys()
    failures = hk.start({"ctrl+alt+s": lambda: None, "ctrl+alt+f12": lambda: None, "ctrl+bogus": lambda: None})
    assert set(created["specs"]) == {"<ctrl>+<alt>+s", "<ctrl>+<alt>+<f12>"}
    assert created["base"] == US_BASE and created["started"] is True  # daemon thread
    assert list(failures) == ["ctrl+bogus"]
    hk.stop()
    assert created["stopped"]


def test_keyboard_package_never_used_on_mac(mac, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyboard", None)  # importing it would now fail loudly
    for kind in ("auto", "keyboard", "pynput"):
        assert isinstance(hotkeys.create_hotkey_backend(kind), hotkeys.PynputHotkeys)


# ------------------------------------------- main-thread-only macOS APIs
@pytest.fixture
def fake_pynput_darwin(mac, fake_modules):
    """Stand-in for pynput's macOS listener internals that records which thread
    calls the keyboard-layout (TIS) APIs and whether the event loop ran."""
    import threading
    from contextlib import contextmanager

    log = {"tis_threads": [], "loop_ran": False, "context_in_loop": "unset"}

    @contextmanager
    def keycode_context():
        log["tis_threads"].append(threading.current_thread())
        yield ("kbd-type", b"layout")

    class ListenerMixin:
        def _run(self):
            log["loop_ran"] = True
            log["context_in_loop"] = self._context

    class DarwinListener(ListenerMixin, threading.Thread):  # what pynput does
        def __init__(self):
            super().__init__(daemon=True)
            self._context = None

        def _run(self):
            with keycode_context() as context:  # on the listener's own thread!
                self._context = context
                try:
                    super()._run()
                finally:
                    self._context = None

        def run(self):
            self._run()

    fake_modules(**{"pynput._util.darwin": {
        "ListenerMixin": ListenerMixin, "keycode_context": keycode_context,
        "keycode_to_string": lambda ctx, vk: "s" if vk == 1 else ""}})
    return log, DarwinListener


def test_stock_macos_listener_calls_tis_off_the_main_thread(fake_pynput_darwin):
    """Documents the pynput behaviour that crashes the app on macOS."""
    import threading

    log, DarwinListener = fake_pynput_darwin
    listener = DarwinListener()
    listener.start()
    listener.join(2)
    assert log["tis_threads"] and log["tis_threads"][0] is not threading.main_thread()


def test_safe_listener_uses_tis_only_on_the_main_thread(fake_pynput_darwin):
    import threading

    log, DarwinListener = fake_pynput_darwin
    safe = backend_mod.safe_listener(DarwinListener)
    assert safe is not DarwinListener and issubclass(safe, DarwinListener)
    for _ in range(5):  # e.g. toggling hotkeys repeatedly
        listener = safe()
        listener.start()
        listener.join(2)
    assert log["tis_threads"] == [threading.main_thread()] * 5  # once per listener, on main
    assert log["loop_ran"] and log["context_in_loop"] == ("kbd-type", b"layout")


def test_safe_listener_elsewhere_keeps_pynputs_own_run(fake_pynput_darwin, monkeypatch):
    import threading

    log, DarwinListener = fake_pynput_darwin
    monkeypatch.setattr(sys, "platform", "linux")  # not macOS: no TIS workaround
    listener = backend_mod.safe_listener(DarwinListener)()
    listener.start()
    listener.join(2)
    assert log["loop_ran"]  # the original _run ran...
    assert log["tis_threads"] and log["tis_threads"][0] is not threading.main_thread()  # ...unchanged


def test_layout_table_is_never_read_off_the_main_thread(fake_pynput_darwin):
    import threading

    log, _ = fake_pynput_darwin
    assert backend_mod.darwin_base_chars() == {1: "s"}
    result = {}
    worker = threading.Thread(target=lambda: result.update(table=backend_mod.darwin_base_chars()))
    worker.start()
    worker.join(2)
    assert result["table"] == {1: "s"}  # the previous table, without calling TIS
    assert log["tis_threads"] == [threading.main_thread()]


def test_stopping_never_calls_cfrunloopstop_with_null(mac, fake_modules):
    """pynput: "if self._loop is not None: CFRunLoopStop(self._loop)" reads _loop twice;
    if the listener thread clears it in between, CFRunLoopStop(NULL) segfaults."""
    calls = []

    class ListenerMixin:
        def _run(self):
            pass

        def _stop_platform(self):  # pynput's code
            if self._loop is not None:
                calls.append(("pynput", self._loop))

    class DarwinListener(ListenerMixin):
        def __init__(self):
            self._running = True

        def stop(self):
            self._running = False
            self._stop_platform()

    fake_modules(**{"pynput._util.darwin": {"ListenerMixin": ListenerMixin,
                                             "keycode_context": None, "keycode_to_string": None},
                    "Quartz": {"CFRunLoopStop": lambda loop: calls.append(("safe", loop))}})

    def exiting_thread(base):
        class ThreadExitsDuringStop(base):
            reads = 0

            @property
            def _loop(self):  # the first read sees the loop, later ones None
                type(self).reads += 1
                return "run-loop" if type(self).reads == 1 else None

            @_loop.setter
            def _loop(self, value):
                pass

        return ThreadExitsDuringStop()

    exiting_thread(DarwinListener).stop()
    assert calls == [("pynput", None)]  # the crash: NULL passed on
    calls.clear()
    exiting_thread(backend_mod.safe_listener(DarwinListener)).stop()
    assert calls == [("safe", "run-loop")]


# ---------------------------------------------------------------- recording
def _recorder(base=US_BASE):
    rec = Recorder(RecordOptions(record_delays=False))
    rec._active, rec._base_chars = True, dict(base)
    return rec


def test_recording_option_shortcut_records_the_key_not_the_symbol():
    rec = _recorder()
    for key, down in ((Key.alt_l, 1), (OPTION_S, 1), (OPTION_S, 0), (Key.alt_l, 0)):
        (rec._press if down else rec._release)(key)
    assert [(a.type, a.params.get("keys")) for a in rec.stop()] == [("key_press", "alt+s")]


def test_recording_shift_typing_keeps_capitals():
    rec = _recorder()
    for key, down in ((Key.shift, 1), (KeyCode(char="S", vk=1), 1), (KeyCode(char="S", vk=1), 0),
                      (Key.shift, 0), (KeyCode(char="o", vk=31), 1), (KeyCode(char="o", vk=31), 0)):
        (rec._press if down else rec._release)(key)
    out = rec.stop()
    assert [(a.type, a.params.get("text")) for a in out] == [("type_text", "So")]


def test_recording_modifier_released_before_the_letter():
    # Without a layout table the letter goes down as "ß", but if Option is released
    # first its key-up arrives as "s". The pair must still match, or the next
    # Option+S would be taken for auto-repeat and lost.
    rec = _recorder(base={})
    for _ in range(2):
        rec._press(Key.alt_l)
        rec._press(OPTION_S)
        rec._release(Key.alt_l)
        rec._release(KeyCode(char="s", vk=1))
    out = rec.stop()
    assert [(a.type, a.params.get("keys")) for a in out] == [("key_press", "alt+ß")] * 2


# ------------------------------------------------------------ key capture
def test_key_capture_maps_mac_modifiers(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from keyboardmanager.gui import widgets

    monkeypatch.setattr(widgets, "_IS_MAC", True)
    # Qt on macOS: Command arrives as Control, Control arrives as Meta
    assert widgets.qt_key_name(Qt.Key.Key_Control) == "cmd"
    assert widgets.qt_key_name(Qt.Key.Key_Meta) == "ctrl"
    w = widgets.KeyCaptureEdit("hotkey")
    w.show()
    w.button.setChecked(True)
    QTest.keyClick(w.button, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    w.button.setChecked(False)  # finish the (single-chord) sequence
    assert w.text() == "alt+cmd+k" and w.normalized() == "alt+cmd+k"
    w.button.setChecked(True)
    QTest.keyClick(w.button, Qt.Key.Key_J, Qt.KeyboardModifier.MetaModifier)
    w.button.setChecked(False)
    assert w.normalized() == "ctrl+j"


# --------------------------------------------------------- files, packaging
def test_database_location(mac, monkeypatch, tmp_path):
    from keyboardmanager.core.database import default_db_path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert default_db_path() == tmp_path / "Library" / "Application Support" / "KeyboardManager" / "keyboardmanager.db"


def _requirements():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    from packaging.requirements import Requirement

    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    return [Requirement(r) for r in data["project"]["dependencies"]]


@pytest.mark.parametrize("platform,expected", [
    ("darwin", {"pyside6", "pynput", "pyobjc-framework-cocoa", "pyobjc-framework-quartz",
                "pyobjc-framework-applicationservices"}),
    ("win32", {"pyside6", "pynput", "keyboard"}),
    ("linux", {"pyside6", "pynput", "keyboard"}),
])
def test_dependencies_per_platform(platform, expected):
    env = {"sys_platform": platform}
    installed = {r.name.lower() for r in _requirements() if r.marker is None or r.marker.evaluate(env)}
    assert installed == expected


# ----------------------------------------------------------- real Mac only
@native
@on_mac
def test_native_frameworks_import():
    import AppKit  # noqa: F401
    import HIServices  # noqa: F401
    import Quartz  # noqa: F401
    from pynput import keyboard, mouse  # noqa: F401


@native
@on_mac
def test_native_layout_table_and_window_lookup():
    table = backend_mod.darwin_base_chars()
    assert set("asdfjkl") <= set(table.values())
    assert isinstance(windowinfo.get_active_window(), ActiveWindow)
    assert all(isinstance(p, permissions.Permission) for p in permissions.missing_permissions())


@native
@on_mac
def test_native_keyboard_package_is_never_imported():
    """Importing `keyboard` aborts the interpreter on macOS, so the app must never do it,
    even when the package is present (e.g. a virtualenv created by an older version)."""
    import subprocess
    import textwrap

    script = textwrap.dedent("""
        import sys
        from keyboardmanager import app
        from keyboardmanager.core import hotkeys
        from keyboardmanager.gui import mainwindow
        for kind in ("auto", "keyboard", "pynput"):
            hotkeys.create_hotkey_backend(kind).start({"ctrl+alt+f12": lambda: None})
        assert "keyboard" not in sys.modules, "keyboard was imported"
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@native
@on_mac
def test_native_package_metadata_excludes_keyboard_on_mac():
    from importlib.metadata import requires

    from packaging.requirements import Requirement

    reqs = [Requirement(r) for r in requires("keyboardmanager") or []]
    active = {r.name.lower() for r in reqs if r.marker is None or r.marker.evaluate()}
    assert "keyboard" not in active and "pyobjc-framework-quartz" in active


@native
@on_mac
def test_native_every_mac_key_maps_to_pynput():
    from keyboardmanager.core import keys

    mac_keys = {"enter", "esc", "tab", "space", "backspace", "delete", "home", "end", "page_up",
                "page_down", "up", "down", "left", "right", "caps_lock", *(f"f{i}" for i in range(1, 21))}
    for name in mac_keys | set("abc123") | set(keys.MODIFIERS):
        backend_mod.name_to_pynput_key(name)
