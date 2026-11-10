"""Vision: image matching, screen-based steps, focusing windows (all with fakes)."""

import subprocess
import sys
import threading

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import executor as executor_mod
from keyboardmanager.core import windowcontrol
from keyboardmanager.core.executor import MacroExecutor
from keyboardmanager.core.interaction import HeadlessInterface
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.preflight import ERROR, INFO, WARNING, CheckContext, check_macro
from keyboardmanager.core.vision import Image, Screen, color_close, find, format_color, parse_color
from tests_support import wait_until

R, G, B = (250, 0, 0), (0, 250, 0), (0, 0, 250)
W, K = (255, 255, 255), (0, 0, 0)


def solid(width, height, color=W):
    return Image.build([[color] * width] * height)


def with_patch(screen: Image, template: Image, x: int, y: int, scale=None) -> Image:
    """``screen`` with ``template`` pasted at (x, y)."""
    rows = [[screen.pixel(i, j) for i in range(screen.width)] for j in range(screen.height)]
    for j in range(template.height):
        for i in range(template.width):
            rows[y + j][x + i] = template.pixel(i, j)
    built = Image.build(rows)
    if scale is not None:
        return Screen(built.width, built.height, built.pixels, scale)
    return built


# ------------------------------------------------------------------- images
def test_image_build_pixel_and_row():
    img = Image.build([[R, G], [B, W]])
    assert (img.width, img.height) == (2, 2)
    assert img.pixel(0, 0) == R and img.pixel(1, 1) == W
    assert img.row(1) == bytes(B + W) and img.row(0, 1) == bytes(G)
    with pytest.raises(ValueError, match="outside"):
        img.pixel(2, 0)
    with pytest.raises(ValueError, match="expected"):
        Image(2, 2, b"123")
    with pytest.raises(ValueError, match="at least one pixel"):
        Image(0, 1, b"")


@pytest.mark.parametrize("x, y", [(0, 0), (3, 2), (17, 12), (0, 12), (17, 0)])
def test_find_exact_at_every_corner(x, y):
    template = Image.build([[R, G, B], [B, R, G], [G, B, R]])
    screen = with_patch(solid(20, 15), template, x, y)
    match = find(screen, template)
    assert (match.left, match.top, match.width, match.height) == (x, y, 3, 3)
    assert match.center == (round(x + 1.5), round(y + 1.5))


def test_find_returns_none_when_absent_or_too_big():
    template = Image.build([[R]])
    assert find(solid(5, 5), template) is None
    assert find(solid(2, 2, R), solid(3, 3, R)) is None  # template larger than the screen


def test_find_checks_every_row_of_the_template():
    template = Image.build([[R], [G]])
    screen = Image.build([[W, R], [W, B], [R, W], [G, W]])  # R over B is not R over G
    match = find(screen, template)
    assert (match.left, match.top) == (0, 2)
    assert find(Image.build([[R], [B]]), template) is None


def test_find_takes_the_topmost_leftmost_match():
    template = Image.build([[R, R]])
    screen = with_patch(with_patch(solid(10, 6), template, 6, 1), template, 2, 3)
    match = find(screen, template)
    assert (match.left, match.top) == (6, 1)


def test_find_ignores_byte_level_false_positives():
    # The template's bytes appear shifted by one byte inside another pixel pair:
    # (5,10,20)(30,99,99) contains 10,20,30 - not a pixel-aligned match.
    template = Image.build([[(10, 20, 30)]])
    screen = Image.build([[(5, 10, 20), (30, 99, 99), (10, 20, 30)]])
    assert find(screen, template).left == 2


def test_find_does_not_match_across_row_ends():
    # A and B are adjacent in the raw bytes (end of row 0, start of row 1), but not
    # on the screen.
    a, b = (1, 2, 3), (4, 5, 6)
    template = Image.build([[a, b]])
    screen = Image.build([[W, a], [b, W]])
    assert find(screen, template) is None
    assert find(Image.build([[W, a], [b, W], [a, b]]), template).top == 2


def test_find_with_tolerance():
    template = Image.build([[(100, 100, 100), (200, 200, 200)]])
    nearly = Image.build([[(104, 97, 100), (196, 200, 205)]])
    screen = with_patch(solid(8, 4), nearly, 3, 2)
    assert find(screen, template) is None
    assert find(screen, template, tolerance=3) is None  # one channel differs by 5
    match = find(screen, template, tolerance=5)
    assert (match.left, match.top) == (3, 2)
    with pytest.raises(ValueError, match="tolerance"):
        find(screen, template, tolerance=256)


def test_match_center_uses_the_screen_scale():
    template = solid(4, 2, R)
    screen = with_patch(solid(20, 10), template, 8, 4, scale=2.0)
    match = find(screen, template)
    assert match.scale == 2.0
    assert match.center == (5, 2)  # (10, 5) capture pixels -> mouse coordinates


@pytest.mark.parametrize("text, rgb", [
    ("#1a2b3c", (26, 43, 60)), ("1A2B3C", (26, 43, 60)), ("#fff", (255, 255, 255)),
    (" #000 ", (0, 0, 0)),
])
def test_parse_color(text, rgb):
    assert parse_color(text) == rgb


def test_color_helpers():
    assert format_color((26, 43, 60)) == "#1a2b3c"
    assert parse_color(format_color((1, 2, 3))) == (1, 2, 3)
    assert color_close((10, 10, 10), (12, 8, 10), 2)
    assert not color_close((10, 10, 10), (13, 10, 10), 2)
    for bad in ("", "red", "#12345", "#1a2b3g"):
        with pytest.raises(ValueError):
            parse_color(bad)


# ------------------------------------------------------------ window control
def test_activate_needs_a_filter_and_never_raises(monkeypatch):
    assert windowcontrol.activate("", " ") is False
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no xdotool")))
    assert windowcontrol.activate("chrome", "") is False


def test_activate_linux_uses_xdotool(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []

    class Result:
        def __init__(self, code):
            self.returncode = code
            self.stdout = ""

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Result(0 if "--name" in cmd else 1)

    monkeypatch.setattr(subprocess, "run", run)
    assert windowcontrol.activate("chrome", "Inbox")
    assert calls == [["xdotool", "search", "--name", "inbox", "windowactivate"]]
    calls.clear()
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: calls.append(cmd) or Result(1))
    assert not windowcontrol.activate("chrome", "")
    assert calls == [["xdotool", "search", "--class", "chrome", "windowactivate"]]


def test_activate_macos_activates_the_running_app(monkeypatch, fake_modules):
    monkeypatch.setattr(sys, "platform", "darwin")
    activated = []

    class App:
        def __init__(self, name):
            self._name = name

        def localizedName(self):  # noqa: N802 - AppKit API
            return self._name

        def activateWithOptions_(self, options):  # noqa: N802
            activated.append((self._name, options))

    class Workspace:
        @staticmethod
        def runningApplications():  # noqa: N802
            return [App("Finder"), App("Google Chrome")]

    fake_modules(AppKit={
        "NSWorkspace": type("WS", (), {"sharedWorkspace": staticmethod(lambda: Workspace())}),
        "NSApplicationActivateIgnoringOtherApps": 1 << 1,
    })
    assert windowcontrol.activate("chrome")
    assert activated == [("Google Chrome", 2)]
    assert not windowcontrol.activate("emacs")
    assert not windowcontrol.activate("", "only a title")  # macOS needs the application


def test_activate_windows_errors_are_swallowed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert windowcontrol.activate("notepad", "") is False  # no ctypes.windll here


# ------------------------------------------------------------ vision steps
class SeeingUI(HeadlessInterface):
    """Serves a sequence of fake screens and named image files."""

    def __init__(self, screens=(), files=None):
        super().__init__()
        self.screens = list(screens)
        self.files = dict(files or {})
        self.grabs = 0
        self.loads = []

    def grab_screen(self):
        if not self.screens:
            return None
        self.grabs += 1
        return self.screens[min(self.grabs - 1, len(self.screens) - 1)]

    def load_image(self, path):
        self.loads.append(path)
        if path not in self.files:
            raise ValueError(f"cannot read the image file {path}")
        return self.files[path]


def A(t, children=None, else_children=None, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children = children or []
    a.else_children = else_children or []
    return a


TEMPLATE = Image.build([[R, G], [B, W]])


def screen_with_template(x=10, y=5, scale=None):
    return with_patch(solid(30, 20), TEMPLATE, x, y, scale=scale)


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(executor_mod, "IMAGE_POLL_S", 0.01)
    monkeypatch.setattr(executor_mod, "WINDOW_POLL_S", 0.01)


def run(backend, *actions, ui, **kw):
    ex = MacroExecutor(backend, ui=ui, **kw)
    return ex.run(Macro("t", list(actions)))


def test_wait_for_image_finds_the_center(backend):
    ui = SeeingUI([screen_with_template(10, 5)], {"ok.png": TEMPLATE})
    r = run(backend, A("wait_for_image", image="ok.png"), ui=ui)
    assert r.status == "success"
    assert (r.variables["found_x"], r.variables["found_y"], r.variables["timed_out"]) == ("11", "6", "0")
    assert "found at (11, 6)" in r.steps[0].detail


def test_wait_for_image_polls_until_it_appears(backend):
    ui = SeeingUI([solid(30, 20)] * 3 + [screen_with_template()], {"ok.png": TEMPLATE})
    r = run(backend, A("wait_for_image", image="ok.png", timeout_s=5), ui=ui)
    assert r.status == "success" and ui.grabs == 4


def test_wait_for_image_until_it_disappears(backend):
    ui = SeeingUI([screen_with_template()] * 2 + [solid(30, 20)], {"busy.png": TEMPLATE})
    r = run(backend, A("wait_for_image", image="busy.png", until="disappears"), ui=ui)
    assert r.status == "success" and r.variables["timed_out"] == "0"
    assert "gone" in r.steps[0].detail


def test_wait_for_image_timeout(backend):
    ui = SeeingUI([solid(30, 20)], {"ok.png": TEMPLATE})
    r = run(backend, A("wait_for_image", image="ok.png", timeout_s=1), ui=ui)
    assert r.status == "error" and "not on the screen after 1 s" in r.message
    ui = SeeingUI([solid(30, 20)], {"ok.png": TEMPLATE})
    r = run(backend, A("wait_for_image", image="ok.png", timeout_s=1, on_timeout="continue"),
            A("type_text", text="{timed_out}"), ui=ui)
    assert r.status == "success" and ("type", "1") in backend.calls


def test_wait_for_image_tolerance(backend):
    off_by_two = Image.build([[(248, 2, 1), (0, 248, 2)], [(2, 0, 250), (253, 255, 254)]])
    ui = SeeingUI([with_patch(solid(30, 20), off_by_two, 4, 4)], {"ok.png": TEMPLATE})
    assert run(backend, A("wait_for_image", image="ok.png", timeout_s=1), ui=ui).status == "error"
    ui.grabs = 0
    r = run(backend, A("wait_for_image", image="ok.png", timeout_s=1, tolerance=2), ui=ui)
    assert r.status == "success" and r.variables["found_x"] == "5"


def test_click_image_moves_and_clicks(backend):
    ui = SeeingUI([screen_with_template(10, 5)], {"go.png": TEMPLATE})
    r = run(backend, A("click_image", image="go.png", button="right", count=2,
                       offset_x=3, offset_y=-2), ui=ui)
    assert r.status == "success"
    assert backend.calls == [("move", 14, 4), ("click", "right", 2)]
    assert (r.variables["found_x"], r.variables["found_y"]) == ("11", "6")


def test_click_image_move_only_and_scale(backend):
    ui = SeeingUI([screen_with_template(10, 4, scale=2.0)], {"go.png": TEMPLATE})
    r = run(backend, A("click_image", image="go.png", move_only=True), ui=ui)
    assert r.status == "success"
    assert backend.calls == [("move", 6, 2)]  # capture (11, 5) at 2x -> mouse (6, 2)


def test_click_image_timeout(backend):
    ui = SeeingUI([solid(30, 20)], {"go.png": TEMPLATE})
    r = run(backend, A("click_image", image="go.png", timeout_s=1), ui=ui)
    assert r.status == "error" and "did not appear" in r.message and backend.calls == []
    ui.grabs = 0
    r = run(backend, A("click_image", image="go.png", timeout_s=1, on_timeout="continue"), ui=ui)
    assert r.status == "success" and backend.calls == []


def test_image_condition(backend):
    ui = SeeingUI([screen_with_template()], {"ok.png": TEMPLATE, "other.png": solid(3, 3, K)})
    r = run(backend,
            A("if", kind="image", image="ok.png", children=[A("type_text", text="seen")]),
            A("if", kind="image", image="other.png", children=[A("type_text", text="wrong")],
              else_children=[A("type_text", text="not seen")]), ui=ui)
    assert r.status == "success"
    assert [c[1] for c in backend.calls if c[0] == "type"] == ["seen", "not seen"]


def test_image_files_are_loaded_once_per_run(backend):
    ui = SeeingUI([screen_with_template()], {"ok.png": TEMPLATE})
    ex = MacroExecutor(backend, ui=ui)
    macro = Macro("t", [A("wait_for_image", image="ok.png"), A("click_image", image="ok.png")])
    assert ex.run(macro).status == "success"
    assert ui.loads == ["ok.png"]
    assert ex.run(macro).status == "success"
    assert ui.loads == ["ok.png", "ok.png"]  # but read afresh per run: the file may have changed


def test_missing_image_file_and_missing_screen(backend):
    ui = SeeingUI([screen_with_template()], {})
    r = run(backend, A("wait_for_image", image="ok.png"), ui=ui)
    assert r.status == "error" and "cannot read the image file" in r.message
    r = run(backend, A("wait_for_image", image="ok.png"), ui=SeeingUI([], {"ok.png": TEMPLATE}))
    assert r.status == "error" and "cannot see the screen" in r.message


def test_wait_for_image_is_stoppable(backend):
    ui = SeeingUI([solid(30, 20)], {"ok.png": TEMPLATE})
    ex = MacroExecutor(backend, ui=ui)
    holder = {}
    th = threading.Thread(target=lambda: holder.update(
        r=ex.run(Macro("t", [A("wait_for_image", image="ok.png", timeout_s=3600)]))))
    th.start()
    assert wait_until(lambda: ui.grabs > 2)
    ex.stop()
    th.join(2)
    assert holder["r"].status == "stopped"


def test_script_find_and_pixel_commands(backend):
    screen = with_patch(solid(30, 20), TEMPLATE, 10, 5)
    ui = SeeingUI([screen], {"ok.png": TEMPLATE, "no.png": solid(2, 2, K)})
    r = run(backend, A("script", code=(
        'at = find("ok.png")\n'
        'if at:\n'
        '    click(at[0], at[1])\n'
        'missing = find("no.png")\n'
        'color = pixel(10, 5)\n'
        'blank = pixel(0, 0)\n'
    )), ui=ui)
    assert r.status == "success"
    assert backend.calls == [("move", 11, 6), ("click", "left", 1)]
    assert r.variables["missing"] == "[]" and r.variables["at"] == "[11, 6]"
    assert r.variables["color"] == "#fa0000" and r.variables["blank"] == "#ffffff"


def test_script_pixel_uses_the_scale(backend):
    screen = with_patch(solid(30, 20), TEMPLATE, 10, 4, scale=2.0)
    r = run(backend, A("script", code='c = pixel(5, 2)'), ui=SeeingUI([screen]))
    assert r.status == "success" and r.variables["c"] == "#fa0000"


def test_focus_window_step(backend, monkeypatch):
    attempts = []

    def activate(app, title):
        attempts.append((app, title))
        if len(attempts) >= 2:
            backend.window = ActiveWindow("Google Chrome", "Inbox")
        return True

    monkeypatch.setattr(executor_mod.windowcontrol, "activate", activate)
    backend.window = ActiveWindow("Terminal", "bash")
    r = run(backend, A("focus_window", app="chrome", title="inbox"), ui=SeeingUI())
    assert r.status == "success" and r.variables["timed_out"] == "0"
    assert attempts[0] == ("chrome", "inbox") and len(attempts) == 2
    assert 'focused "Google Chrome"' in r.steps[0].detail


def test_focus_window_timeout_and_already_focused(backend, monkeypatch):
    monkeypatch.setattr(executor_mod.windowcontrol, "activate", lambda app, title: False)
    r = run(backend, A("focus_window", app="emacs", timeout_s=1), ui=SeeingUI())
    assert r.status == "error" and 'could not focus "emacs"' in r.message
    monkeypatch.setattr(executor_mod.windowcontrol, "activate",
                        lambda app, title: pytest.fail("already focused: no need to ask the OS"))
    r = run(backend, A("focus_window", app="terminal", title="bash"), ui=SeeingUI())
    assert r.status == "success"


def test_script_focus_command(backend, monkeypatch):
    monkeypatch.setattr(executor_mod.windowcontrol, "activate",
                        lambda app, title: setattr(backend, "window", ActiveWindow("Mail", "Inbox")) or True)
    r = run(backend, A("script", code='ok = focus("mail")\ntype(ok)'), ui=SeeingUI())
    assert r.status == "success" and ("type", "true") in backend.calls


def test_simulated_runs_only_record_focusing(backend, monkeypatch):
    from keyboardmanager.core.debugger import SimulatedBackend

    monkeypatch.setattr(executor_mod.windowcontrol, "activate",
                        lambda app, title: pytest.fail("a simulated run must not touch real windows"))
    simulated = SimulatedBackend(backend)
    ex = MacroExecutor(simulated, ui=SeeingUI())
    r = ex.run(Macro("t", [A("focus_window", app="chrome", title="inbox"),
                           A("script", code='ok = focus("mail")' + chr(10) + 'log(ok)')]))
    assert r.status == "success" and r.variables["timed_out"] == "0" and r.variables["ok"] == "true"
    assert [t for _, t in simulated.sent] == ["focus window 'chrome / inbox'", "focus window 'mail'"]


def test_switch_layer_and_profile_need_the_service(backend):
    r = run(backend, A("switch_layer", layer="nav"), ui=SeeingUI())
    assert r.status == "error" and "running application" in r.message
    r = run(backend, A("switch_profile", profile="Work"), ui=SeeingUI())
    assert r.status == "error" and "running application" in r.message
    calls = []
    r = run(backend, A("switch_layer", layer="nav", one_shot=True), A("switch_profile", profile="Work"),
            A("script", code='layer("x")\nprofile("")'), ui=SeeingUI(),
            set_layer=lambda n, o: calls.append(("layer", n, o)),
            set_profile=lambda n: calls.append(("profile", n)))
    assert r.status == "success"
    assert calls == [("layer", "nav", True), ("profile", "Work"), ("layer", "x", False), ("profile", "")]


# --------------------------------------------------------------- pre-flight
def test_preflight_of_vision_steps(tmp_path):
    good = tmp_path / "ok.png"
    good.write_bytes(b"png")
    macro = Macro("V", [A("wait_for_image", image=str(good)),
                        A("click_image", image=str(tmp_path / "missing.png")),
                        A("if", kind="image", image="{shot}"),
                        A("switch_layer", layer="nav")])
    issues = [(i.severity, i.message, i.blocks_run) for i in check_macro(macro, CheckContext(macros=[macro]))]
    assert (ERROR, f"there is no image file {tmp_path / 'missing.png'}", True) in issues
    assert not any("ok.png" in m for _, m, _ in issues)
    assert not any("{shot}" in m for _, m, _ in issues)  # placeholders: only known at run time
    assert any(s == WARNING and 'no macro is on layer "nav"' in m for s, m, _ in issues)


def test_preflight_layer_notes():
    on_layer = Macro("L", [A("type_text", text="x")], hotkey="ctrl+1", layer="nav")
    pointless = Macro("P", [A("type_text", text="x")], layer="nav")
    context = CheckContext(macros=[on_layer, pointless])
    notes = [i.message for i in check_macro(on_layer, context) if i.severity == INFO]
    assert any('on layer "nav"' in m for m in notes)
    notes = [i.message for i in check_macro(pointless, context) if i.severity == INFO]
    assert any("has no effect" in m for m in notes)
