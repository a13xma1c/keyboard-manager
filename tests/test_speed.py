"""Playback speed: macro speed x global speed, applied to delays and typing, live."""

import threading
import time

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.executor import MacroExecutor
from keyboardmanager.core.models import Macro
from keyboardmanager.core.service import MacroService
from tests_support import FakeHotkeys, wait_until


def A(t, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    return a


def timed(backend, macro, speed=lambda: 1.0):
    ex = MacroExecutor(backend, speed=speed)
    t = time.monotonic()
    result = ex.run(macro)
    return time.monotonic() - t, result


@pytest.mark.parametrize("macro_speed,global_speed,expected", [(1, 1, 0.4), (2, 1, 0.2), (1, 4, 0.1), (2, 0.5, 0.4)])
def test_delays_scale_with_both_speeds(backend, macro_speed, global_speed, expected):
    elapsed, result = timed(backend, Macro("m", [A("delay", ms=400)], speed=macro_speed), lambda: global_speed)
    assert result.status == "success"
    assert expected * 0.8 <= elapsed <= expected + 0.25


def test_typing_interval_scales(backend):
    elapsed, _ = timed(backend, Macro("m", [A("type_text", text="abcd", interval_ms=100)], speed=4))
    assert elapsed < 0.3  # 4 x 100 ms at 4x = 0.1 s
    assert "".join(c[1] for c in backend.calls) == "abcd"


def test_speed_change_applies_in_the_middle_of_a_delay(backend):
    speed = [1.0]
    ex = MacroExecutor(backend, speed=lambda: speed[0])
    done = {}
    th = threading.Thread(target=lambda: done.update(r=ex.run(Macro("m", [A("delay", ms=4000)]))))
    t = time.monotonic()
    th.start()
    time.sleep(0.3)
    speed[0] = 10.0  # the remaining ~3.7 s of macro time now take ~0.37 s
    th.join(5)
    assert done["r"].status == "success" and time.monotonic() - t < 1.5


def test_pause_and_stop_still_work_at_any_speed(backend):
    ex = MacroExecutor(backend, speed=lambda: 0.1)  # very slow: a 1 s delay takes 10 s
    done = {}
    th = threading.Thread(target=lambda: done.update(r=ex.run(Macro("m", [A("delay", ms=1000)]))))
    th.start()
    time.sleep(0.2)
    ex.pause()
    time.sleep(0.2)
    ex.stop()
    th.join(2)
    assert done["r"].status == "stopped"


def test_timeouts_are_real_time_not_scaled(backend, monkeypatch):
    """A window wait's timeout is about the outside world, so speed does not change it."""
    from keyboardmanager.core import executor as executor_mod

    monkeypatch.setattr(executor_mod, "WINDOW_POLL_S", 0.05)
    elapsed, result = timed(backend, Macro("m", [A("wait_for_window", value="never", timeout_s=1,
                                                   on_timeout="continue")], speed=10))
    assert result.status == "success" and elapsed >= 0.9


def test_bad_speed_values_fall_back(backend):
    ex = MacroExecutor(backend, speed=lambda: "nonsense")
    ex.run(Macro("m", [A("delay", ms=10)], speed=2))
    assert ex.speed_factor() == 2.0


def test_service_playback_speed(db, backend):
    svc = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    assert svc.playback_speed == 1.0
    svc.set_playback_speed(2.5)
    with pytest.raises(ValueError):
        svc.set_playback_speed(0)
    assert MacroService(db, backend, FakeHotkeys(), scheduler_thread=False).playback_speed == 2.5  # persisted
    db.set_setting("playback_speed", "garbage")
    assert MacroService(db, backend, FakeHotkeys(), scheduler_thread=False).playback_speed == 1.0
    svc.set_playback_speed(4)
    t = time.monotonic()
    svc.run(db.save_macro(Macro("m", [A("delay", ms=800)])))
    assert wait_until(lambda: svc.running_macro is None)
    assert time.monotonic() - t < 0.6  # 0.8 s at 4x
    svc.shutdown()
