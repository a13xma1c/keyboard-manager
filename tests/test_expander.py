"""Text expansion: abbreviation rules, typed-text tracking, listener wiring and the
service replacing what was typed."""

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.expander import (
    BUFFER_SIZE,
    TextExpander,
    abbreviations_conflict,
    normalize_abbreviation,
)
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.remap import Remap
from keyboardmanager.core.service import MacroService, expansion_macro
from tests_support import FakeHotkeys, wait_until


@pytest.mark.parametrize("text,expected", [(" ;sig ", ";sig"), ("", ""), ("btw", "btw"), ("ü!", "ü!")])
def test_normalize_abbreviation(text, expected):
    assert normalize_abbreviation(text) == expected


@pytest.mark.parametrize("text", [";", "a b", "x" * 33, "tab\there"])
def test_invalid_abbreviations(text):
    with pytest.raises(ValueError):
        normalize_abbreviation(text)


def test_abbreviation_conflicts():
    assert abbreviations_conflict(";s", ";sig") and abbreviations_conflict(";sig", ";s")
    assert abbreviations_conflict(";sig", ";sig")
    assert not abbreviations_conflict(";sig", ";adr") and not abbreviations_conflict("sig", ";sig")


# ------------------------------------------------------------ typed text logic
@pytest.fixture
def expander():
    found = []
    e = TextExpander(on_match=found.append)
    e.update({";sig", ";adr", "btw", ";sig2"}, listen=False)
    e.found = found
    return e


def type_(e, text, injected=False):
    for ch in text:
        e.key("space" if ch == " " else ch.lower(), ch, True, injected)
        e.key("space" if ch == " " else ch.lower(), ch, False, injected)


def test_typing_an_abbreviation_is_reported_at_once(expander):
    type_(expander, "hello ;si")
    assert expander.found == []
    type_(expander, "g")
    assert expander.found == [";sig"]
    type_(expander, "2")  # the typed text was consumed: ";sig2" is not triggered after the fact
    assert expander.found == [";sig"]


def test_matching_is_case_sensitive_and_works_mid_word(expander):
    type_(expander, ";SIG")
    assert expander.found == []
    type_(expander, "xbtw")  # no word boundary needed
    assert expander.found == ["btw"]


def test_backspace_corrects_a_typo(expander):
    type_(expander, ";sx")
    expander.key("backspace", None, True)
    type_(expander, "ig")
    assert expander.found == [";sig"]


@pytest.mark.parametrize("interrupt", [
    lambda e: e.key("space", " ", True),
    lambda e: e.key("enter", None, True),
    lambda e: e.key("left", None, True),  # the cursor moved
    lambda e: e.click(),
    lambda e: (e.key("ctrl", None, True), e.key("v", "v", True), e.key("ctrl", None, False)),  # a paste
])
def test_anything_that_may_move_the_cursor_forgets_the_typed_text(expander, interrupt):
    type_(expander, ";s")
    interrupt(expander)
    type_(expander, "ig")
    assert expander.found == []


def test_shift_and_alt_are_typing_not_shortcuts(expander):
    expander.key("shift", None, True)
    type_(expander, ";a")
    expander.key("shift", None, False)
    expander.key("alt", None, True)
    type_(expander, "dr")
    assert expander.found == [";adr"]


def test_own_keystrokes_are_ignored(expander):
    type_(expander, ";sig", injected=True)  # e.g. a macro typing the text
    assert expander.found == []


def test_memory_is_bounded_and_reset_by_updates(expander):
    type_(expander, "x" * (BUFFER_SIZE * 2))
    assert len(expander._typed) == BUFFER_SIZE
    type_(expander, ";si")
    expander.update({";sig"}, listen=False)
    type_(expander, "g")
    assert expander.found == []


def test_longest_abbreviation_wins():
    found = []
    e = TextExpander(on_match=found.append)
    e.update({"sig", "xsig"}, listen=False)  # not prefixes of each other, but both end the text
    type_(e, "xsig")
    assert found == ["xsig"]


# ------------------------------------------------------------ pynput wiring
class FakeListener:
    made: list = []

    def __init__(self, **callbacks):
        self.callbacks, self.started, self.stopped, self.daemon = callbacks, False, False, False
        FakeListener.made.append(self)

    def start(self): self.started = True
    def stop(self): self.stopped = True


def test_listeners_translate_pynput_events(monkeypatch):
    from pynput import keyboard, mouse
    from pynput.keyboard import Key, KeyCode
    from pynput.mouse import Button

    FakeListener.made = []
    monkeypatch.setattr(keyboard, "Listener", FakeListener)
    monkeypatch.setattr(mouse, "Listener", FakeListener)
    found = []
    e = TextExpander(on_match=found.append)
    e.update({";ok"})
    kb, ms = FakeListener.made
    assert kb.started and ms.started and kb.daemon
    for key in (KeyCode.from_char(";"), KeyCode.from_char("x")):
        kb.callbacks["on_press"](key, False)
    kb.callbacks["on_press"](Key.backspace, False)
    kb.callbacks["on_press"](KeyCode.from_char("o"), True)  # injected: ignored
    kb.callbacks["on_press"](KeyCode.from_char("o"), False)
    ms.callbacks["on_click"](1, 1, Button.left, False)  # a release is not a click
    kb.callbacks["on_press"](KeyCode.from_char("k"), False)
    assert found == [";ok"]
    e.update(set())  # nothing left to watch: listeners stop
    assert kb.stopped and ms.stopped
    e.update({";ok"})
    assert len(FakeListener.made) == 4  # fresh listeners when needed again


# ------------------------------------------------------------------ service
@pytest.fixture
def svc(db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    yield service
    service.shutdown()


def snippet(db, abbreviation, text, **kw):
    step = catalog.new_action("type_text")
    step.params["text"] = text
    return db.save_macro(Macro(f"Snippet {abbreviation}", [step], abbreviation=abbreviation, **kw))


def test_typing_an_abbreviation_replaces_it(svc, db, backend):
    snippet(db, ";sig", "Best regards")
    svc.reload()
    assert svc.expander.abbreviations == [";sig"]
    type_(svc.expander, ";sig")
    assert wait_until(lambda: svc.running_macro is None and ("type", "Best regards") in backend.calls)
    presses = [c for c in backend.calls if c == ("down", "backspace")]
    assert len(presses) == 4  # one per typed character, before the text
    assert backend.calls.index(("type", "Best regards")) > backend.calls.index(("down", "backspace"))
    entry = db.list_history()[0]
    assert (entry.macro_name, entry.trigger, entry.status) == ("Snippet ;sig", "abbreviation ;sig", "success")


def test_app_specific_snippet_wins_and_filters_apply(svc, db, backend):
    snippet(db, ";x", "global text")
    step = catalog.new_action("type_text")
    step.params["text"] = "mail text"
    db.save_macro(Macro("mail only", [step], abbreviation=";x", app_filter="mail"))  # same abbreviation
    svc.reload()
    backend.window = ActiveWindow("Mail", "")
    type_(svc.expander, ";x")
    assert wait_until(lambda: svc.running_macro is None and backend.calls)
    assert ("type", "mail text") in backend.calls and ("type", "global text") not in backend.calls
    backend.calls.clear()
    backend.window = ActiveWindow("Terminal", "")
    type_(svc.expander, ";x")
    assert wait_until(lambda: svc.running_macro is None and backend.calls)
    assert ("type", "global text") in backend.calls


def test_global_hotkeys_switch_also_covers_abbreviations(svc, db):
    snippet(db, ";sig", "x")
    snippet(db, ";off", "x", enabled=False)
    svc.reload()
    assert svc.expander.abbreviations == [";sig"]
    svc.set_hotkeys_enabled(False)
    assert svc.expander.abbreviations == []


def test_expansion_macro_erases_then_runs():
    step = catalog.new_action("stop")
    original = Macro("m", [step], abbreviation="abc", id=7)
    copy = expansion_macro(original, "abc")
    assert copy.id == 7 and [a.type for a in copy.actions] == ["delay", "repeat", "stop"]
    assert copy.actions[1].params["count"] == 3
    assert copy.actions[1].children[0].params["keys"] == "backspace"
    assert original.actions == [step]  # the stored macro is untouched


# ---------------------------------------------------------------- remapping
def test_remaps_are_stored_applied_and_cleared(svc, no_real_remapping):
    remapper = svc.remapper
    assert svc.remaps == [] and svc.remapping_enabled
    assert svc.set_remaps([Remap("CapsLock", "Escape")]) == {}
    assert svc.remaps == [Remap("caps_lock", "esc")] and remapper.applied[-1] == svc.remaps
    with pytest.raises(ValueError):
        svc.set_remaps([Remap("a", "a")])
    assert svc.remaps == [Remap("caps_lock", "esc")]  # unchanged
    svc.set_remapping_enabled(False)
    assert not svc.remapping_enabled and remapper.cleared >= 1
    cleared = remapper.cleared
    svc.shutdown()
    assert remapper.cleared == cleared + 1  # hidutil mappings must not outlive the app


def test_corrupt_remap_setting_is_ignored(svc, db):
    db.set_setting("key_remaps", "{garbage")
    assert svc.remaps == []
    assert svc.apply_remaps() == {}


def test_remapper_problems_never_break_the_app(db, backend):
    class Broken:
        name = "broken"

        def unavailable_reason(self): return None
        def apply(self, remaps): raise RuntimeError("driver exploded")
        def clear(self): raise RuntimeError("still exploded")
        def recover(self): raise RuntimeError("and again")

    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False, remapper=Broken())
    failures = service.set_remaps([Remap("caps_lock", "esc")])
    assert "driver exploded" in failures["*"]
    service.shutdown()  # must not raise


def test_a_shortcut_is_not_part_of_the_typed_text(expander):
    """';s', then Ctrl+I, then 'g' must not count as typing ';sig'."""
    type_(expander, ";s")
    expander.key("ctrl", None, True)
    expander.key("i", "i", True)
    expander.key("i", "i", False)
    expander.key("ctrl", None, False)
    type_(expander, "g")
    assert expander.found == []
    expander.key("cmd", None, True)  # the same for Cmd (macOS)
    type_(expander, ";sig")
    expander.key("cmd", None, False)
    assert expander.found == []


def test_service_start_runs_remap_crash_recovery(db, backend):
    class Recovering:
        name = "recovering"
        recovered = 0

        def unavailable_reason(self): return None
        def apply(self, remaps): return {}
        def clear(self): pass

        def recover(self):
            Recovering.recovered += 1

    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False, remapper=Recovering())
    assert Recovering.recovered == 1  # a mapping left by a crash is undone at once
    service.shutdown()
