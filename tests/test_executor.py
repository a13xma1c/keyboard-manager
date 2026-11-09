import random
import threading
import time
from datetime import datetime

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.executor import MacroExecutor
from keyboardmanager.core.models import Action, ActiveWindow, Macro


def A(t, children=None, else_children=None, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children = children or []
    a.else_children = else_children or []
    return a


def run(backend, *actions, now=None, variables=None):
    ex = MacroExecutor(backend, now=now or datetime.now)
    return ex.run(Macro("t", list(actions)), variables)


def test_key_press_order_and_release(backend):
    r = run(backend, A("key_press", keys="shift+ctrl+t"))
    assert r.status == "success" and r.actions_run == 1
    assert backend.calls == [("down", "ctrl"), ("down", "shift"), ("down", "t"),
                             ("up", "t"), ("up", "shift"), ("up", "ctrl")]


def test_type_text_and_mouse(backend):
    run(backend, A("type_text", text="hi"), A("mouse_move", x=5, y=6),
        A("mouse_move", x=1, y=1, relative=True),
        A("mouse_click", button="right", count=2, move_to=True, x=9, y=9),
        A("mouse_scroll", dx=0, dy=-2))
    assert backend.calls == [("type", "hi"), ("move", 5, 6), ("move", 6, 7), ("move", 9, 9),
                             ("click", "right", 2), ("scroll", 0, -2)]


def test_type_text_with_interval_types_per_char(backend):
    run(backend, A("type_text", text="ab", interval_ms=1))
    assert backend.calls == [("type", "a"), ("type", "b")]


def test_repeat_and_iteration_variable(backend):
    r = run(backend, A("repeat", count=3, children=[A("key_press", keys="a")]))
    assert [c for c in backend.calls if c[0] == "down"] == [("down", "a")] * 3
    assert r.variables["iteration"] == "3"
    assert r.actions_run == 4


def test_if_app_condition_then_else(backend):
    cond = lambda: A("if", kind="app", value="term",  # noqa: E731
                     children=[A("key_press", keys="a")], else_children=[A("key_press", keys="b")])
    run(backend, cond())
    backend.window = ActiveWindow("Chrome", "x")
    run(backend, cond())
    assert [c[1] for c in backend.calls if c[0] == "down"] == ["a", "b"]


def test_if_negate_and_title(backend):
    run(backend, A("if", kind="title", value="bash", negate=True, children=[A("key_press", keys="a")],
                   else_children=[A("key_press", keys="b")]))
    assert backend.calls[0] == ("down", "b")


def test_if_time_range_including_overnight(backend):
    def at(h, m):
        return lambda: datetime(2024, 1, 1, h, m)

    prog = lambda: A("if", kind="time", value="22:00-06:00", children=[A("key_press", keys="a")])  # noqa: E731
    run(backend, prog(), now=at(23, 0))
    run(backend, prog(), now=at(3, 0))
    run(backend, prog(), now=at(12, 0))
    assert len([c for c in backend.calls if c[0] == "down"]) == 2


def test_variables_and_conditions(backend):
    run(backend,
        A("set_variable", name="n", value="1"),
        A("repeat", count=3, children=[A("set_variable", name="n", value="2", mode="add")]),
        A("if", kind="variable", variable="n", op="==", compare_to="7", children=[A("key_press", keys="a")],
          else_children=[A("key_press", keys="z")]))
    assert backend.calls[0] == ("down", "a")


def test_variable_comparisons(backend):
    run(backend, A("if", kind="variable", variable="v", op=">", compare_to="9",
                   children=[A("key_press", keys="a")]), variables={"v": "10"})
    run(backend, A("if", kind="variable", variable="v", op="contains", compare_to="LL",
                   children=[A("key_press", keys="b")]), variables={"v": "hello"})
    assert [c[1] for c in backend.calls if c[0] == "down"] == ["a", "b"]


def test_add_to_non_number_is_an_error(backend):
    r = run(backend, A("set_variable", name="n", value="abc", mode="add"))
    assert r.status == "error" and "not a number" in r.message


def test_stop_action_ends_macro(backend):
    r = run(backend, A("key_press", keys="a"), A("stop"), A("key_press", keys="b"))
    assert r.status == "stopped"
    assert [c for c in backend.calls if c[0] == "down"] == [("down", "a")]


def test_delay_is_measured_and_jitter_bounded(backend):
    t = time.monotonic()
    run(backend, A("delay", ms=60, jitter_ms=0))
    assert time.monotonic() - t >= 0.055
    ex = MacroExecutor(backend, rng=random.Random(1))
    t = time.monotonic()
    ex.run(Macro("t", [A("delay", ms=0, jitter_ms=50)]))
    assert time.monotonic() - t < 0.2


def test_stop_interrupts_delay_and_releases_held_keys(backend):
    ex = MacroExecutor(backend)
    macro = Macro("t", [A("key_down", key="shift"), A("mouse_click", button="left", state="press"),
                        A("delay", ms=10_000)])
    holder = {}
    th = threading.Thread(target=lambda: holder.update(r=ex.run(macro)))
    th.start()
    time.sleep(0.15)
    ex.stop()
    th.join(2)
    assert not th.is_alive()
    assert holder["r"].status == "stopped"
    assert ("up", "shift") in backend.calls and ("mup", "left") in backend.calls


def test_infinite_repeat_stoppable_even_when_empty(backend):
    ex = MacroExecutor(backend)
    holder = {}
    th = threading.Thread(target=lambda: holder.update(r=ex.run(Macro("t", [A("repeat", count=0)]))))
    th.start()
    time.sleep(0.1)
    ex.stop()
    th.join(2)
    assert holder["r"].status == "stopped"


def test_invalid_macro_reports_error_without_input(backend):
    r = run(backend, Action("key_press", {"keys": "nope+"}))
    assert r.status == "error" and backend.calls == []


def test_backend_failure_is_reported_and_keys_released(backend):
    def boom(key):
        if key == "b":
            raise RuntimeError("denied")
        backend.calls.append(("down", key))

    backend.key_down = boom
    r = run(backend, A("key_press", keys="ctrl+b"))
    assert r.status == "error" and "denied" in r.message
    assert ("up", "ctrl") in backend.calls


# ------------------------------------------------------------------ pause / resume
def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def start(ex, *actions, **kw):
    holder = {}
    th = threading.Thread(target=lambda: holder.update(r=ex.run(Macro("t", list(actions)), **kw)))
    th.start()
    return th, holder


def test_pause_between_steps_and_resume(backend):
    ex = MacroExecutor(backend)
    th, holder = start(ex, A("type_text", text="abcdefghij", interval_ms=20))
    assert wait_until(lambda: len(backend.calls) >= 2)
    ex.pause()
    time.sleep(0.05)  # let the current character finish
    frozen = len(backend.calls)
    time.sleep(0.15)
    assert len(backend.calls) == frozen and th.is_alive() and ex.paused
    ex.resume()
    th.join(2)
    assert holder["r"].status == "success"
    assert "".join(c[1] for c in backend.calls) == "abcdefghij"


def test_pause_freezes_remaining_delay(backend):
    ex = MacroExecutor(backend)
    th, holder = start(ex, A("delay", ms=200), A("key_press", keys="a"))
    time.sleep(0.05)
    ex.pause()
    time.sleep(0.3)  # well past the original delay
    assert th.is_alive() and backend.calls == []
    resumed = time.monotonic()
    ex.resume()
    th.join(2)
    # roughly 150 ms of the delay were still left after resuming
    assert time.monotonic() - resumed >= 0.1
    assert holder["r"].status == "success" and ("down", "a") in backend.calls


def test_pause_releases_held_input_and_resume_restores_it(backend):
    ex = MacroExecutor(backend)
    th, holder = start(ex, A("key_down", key="shift"), A("mouse_click", button="left", state="press"),
                       A("delay", ms=150), A("key_press", keys="a"))
    assert wait_until(lambda: ("mdown", "left") in backend.calls)
    ex.pause()
    assert wait_until(lambda: ("mup", "left") in backend.calls)
    assert backend.calls[-2:] == [("up", "shift"), ("mup", "left")]
    backend.calls.clear()
    ex.resume()
    th.join(2)
    assert holder["r"].status == "success"
    # pressed again on resume, then the step runs, then released at the end
    assert backend.calls == [("down", "shift"), ("mdown", "left"), ("down", "a"), ("up", "a"),
                             ("up", "shift"), ("mup", "left")]


def test_stop_while_paused(backend):
    ex = MacroExecutor(backend)
    th, holder = start(ex, A("key_down", key="ctrl"), A("delay", ms=10_000), A("key_press", keys="a"))
    assert wait_until(lambda: backend.calls)
    ex.pause()
    time.sleep(0.05)
    ex.stop()
    th.join(2)
    assert not th.is_alive() and holder["r"].status == "stopped"
    assert ("down", "a") not in backend.calls
    assert backend.calls.count(("up", "ctrl")) == 1  # released on pause, not re-pressed


def test_pause_and_stop_requested_before_run(backend):
    ex = MacroExecutor(backend)
    ex.pause()
    th, holder = start(ex, A("key_press", keys="a"))
    time.sleep(0.1)
    assert th.is_alive() and backend.calls == []
    ex.resume()
    th.join(2)
    assert holder["r"].status == "success"

    ex = MacroExecutor(backend)
    ex.stop()  # must not be lost when run() starts afterwards
    r = ex.run(Macro("t", [A("key_press", keys="b")]))
    assert r.status == "stopped" and r.message == "Stopped before start"
    # the executor is reusable once a run has ended
    assert not ex.stopped and not ex.paused
    assert ex.run(Macro("t", [A("key_press", keys="b")])).status == "success"


def test_start_delay_is_pausable(backend):
    ex = MacroExecutor(backend)
    th, holder = start(ex, A("key_press", keys="a"), start_delay=0.15)
    time.sleep(0.05)
    ex.pause()
    time.sleep(0.25)
    assert th.is_alive() and backend.calls == []
    ex.resume()
    th.join(2)
    assert holder["r"].status == "success"
