"""Profiles, keymap layers and context rules (core and service level)."""

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import context as context_mod
from keyboardmanager.core.context import ContextRule, ContextWatcher, rules_from_json, rules_to_json, validate_rule
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.profiles import ProfileStore, in_profile, normalize_profile, normalize_profiles
from keyboardmanager.core.service import MacroService
from keyboardmanager.core.triggers import TriggerRule
from tests_support import FakeHotkeys, wait_until


def A(t, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    return a


def typing(name, text=None, **kw):
    return Macro(name, [A("type_text", text=text or name)], **kw)


@pytest.fixture
def svc(db, backend):
    events = []
    service = MacroService(db, backend, FakeHotkeys(), listener=lambda e, p: events.append((e, p)),
                           scheduler_thread=False)
    service.events_seen = events
    yield service
    service.shutdown()


def typed(backend):
    return [c[1] for c in backend.calls if c[0] == "type"]


def idle(svc):
    return wait_until(lambda: svc.running_macro is None)


# ------------------------------------------------------------------ profiles
def test_normalize_profiles():
    assert normalize_profiles("Work,  game , work, ") == ["Work", "game"]
    assert normalize_profiles(["A  B"]) == ["A B"]
    assert normalize_profile("  Deep   Work ") == "Deep Work"
    with pytest.raises(ValueError, match="comma"):
        normalize_profile("a,b")
    with pytest.raises(ValueError, match="text"):
        normalize_profiles([3])  # type: ignore[list-item]


def test_in_profile():
    assert in_profile([], "Work")          # unrestricted macro: in every profile
    assert in_profile(["Work"], "")        # "All profiles": everything is active
    assert in_profile(["Work", "Game"], "work")
    assert not in_profile(["Work"], "Game")


def test_profile_store():
    box = {"text": ""}
    store = ProfileStore(load=lambda: box["text"], save=lambda t: box.__setitem__("text", t))
    assert store.all() == []
    store.add("Work")
    store.add("Gaming")
    with pytest.raises(ValueError, match="already"):
        store.add("work")
    with pytest.raises(ValueError, match="name"):
        store.add("  ")
    assert store.all() == ["Gaming", "Work"]
    assert store.all(["Writing", "work"]) == ["Gaming", "Work", "Writing"]  # macros' profiles count
    store.rename("gaming", "Games")
    with pytest.raises(ValueError, match="already"):
        store.rename("Games", "work")
    store.rename("Games", "GAMES")  # case change of itself is fine
    assert store.all() == ["GAMES", "Work"]
    store.remove("games")
    assert store.all() == ["Work"]


def test_profile_gates_hotkeys_and_abbreviations(svc, db, backend):
    db.save_macro(typing("everywhere", hotkey="ctrl+1"))
    db.save_macro(typing("work only", hotkey="ctrl+2", profiles=["Work"], abbreviation=";wo"))
    svc.reload()
    assert svc.active_profile == ""
    assert {"ctrl+1", "ctrl+2"} <= set(svc.hotkeys.mapping)
    svc.set_active_profile("Home")
    assert "ctrl+2" not in svc.hotkeys.mapping and "ctrl+1" in svc.hotkeys.mapping
    svc.on_hotkey("ctrl+2")  # not registered any more; even a stale call does nothing
    assert typed(backend) == []
    assert ("profile", "Home") in svc.events_seen
    svc.set_active_profile("work")
    assert "ctrl+2" in svc.hotkeys.mapping
    svc.on_hotkey("ctrl+2")
    assert idle(svc) and typed(backend) == ["work only"]


def test_unknown_profile_is_created_when_switched_to(svc):
    svc.set_active_profile("Brand New")
    assert svc.all_profiles() == ["Brand New"]
    assert svc.active_profile == "Brand New"
    svc.set_active_profile("")
    assert svc.active_profile == "" and svc.all_profiles() == ["Brand New"]


def test_profile_gates_schedules_events_and_triggers(svc, db, backend):
    from keyboardmanager.core.schedule import Schedule

    db.save_macro(typing("scheduled", profiles=["Work"], schedule=Schedule("interval", minutes=5)))
    db.save_macro(typing("on start", profiles=["Work"], event="app_start"))
    db.save_macro(typing("Build"))
    svc.save_trigger(TriggerRule("after", "macro_finished", "scheduled", {"macro": "Build"}))
    svc.set_active_profile("Home")
    assert svc.scheduler.upcoming() == []
    svc.fire_startup_event()
    svc.run(svc.find_macro("Build"), blocking=True)
    assert wait_until(lambda: svc.triggers.status)
    assert "not in the active profile" in list(svc.triggers.status.values())[0]
    assert idle(svc) and typed(backend) == ["Build"]
    svc.set_active_profile("Work")
    assert len(svc.scheduler.upcoming()) == 1
    svc.fire_startup_event()
    assert idle(svc) and typed(backend) == ["Build", "on start"]


def test_switch_profile_step(svc, db, backend):
    macro = Macro("go work", [A("switch_profile", profile="Work")])
    svc.run(macro, blocking=True)
    assert svc.active_profile == "Work" and svc.chosen_profile == "Work"
    assert ("profile", "Work") in svc.events_seen
    svc.run(Macro("back", [A("switch_profile", profile="")]), blocking=True)
    assert svc.active_profile == ""


def test_rename_profile_updates_macros_rules_and_choice(svc, db):
    m = db.save_macro(typing("m", profiles=["Work", "Other"]))
    svc.profiles.add("Work")
    svc.set_context_rules([ContextRule(app="code", profile="Work"), ContextRule(app="x", layer="nav")])
    svc.set_active_profile("Work")
    svc.rename_profile("work", "Office")
    assert db.get_macro(m.id).profiles == ["Office", "Other"]
    assert svc.context_rules[0].profile == "Office" and svc.context_rules[1].profile is None
    assert svc.active_profile == "Office"
    assert "Office" in svc.all_profiles() and "Work" not in svc.all_profiles()


def test_delete_profile_lifts_the_restriction(svc, db):
    m = db.save_macro(typing("m", profiles=["Work"]))
    svc.profiles.add("Work")
    svc.set_context_rules([ContextRule(app="code", profile="Work")])
    svc.set_active_profile("Work")
    svc.delete_profile("Work")
    assert db.get_macro(m.id).profiles == []
    assert svc.context_rules == []  # the rule only switched the profile: gone with it
    assert svc.active_profile == "" and svc.all_profiles() == []


def test_delete_profile_keeps_rules_that_also_switch_a_layer(svc):
    svc.set_context_rules([ContextRule(app="code", profile="Work", layer="nav")])
    svc.delete_profile("Work")
    # still useful for its layer; it no longer touches the profile
    assert svc.context_rules == [ContextRule(app="code", profile=None, layer="nav")]


# -------------------------------------------------------------------- layers
def test_layer_gates_hotkeys_and_wins_over_base(svc, db, backend):
    db.save_macro(typing("base", hotkey="ctrl+1"))
    db.save_macro(typing("nav version", hotkey="ctrl+1", layer="Nav"))
    db.save_macro(typing("nav only", hotkey="ctrl+2", layer="Nav", abbreviation=";n"))
    svc.reload()
    assert svc.active_layer == "" and svc.all_layers() == ["Nav"]
    assert "ctrl+2" not in svc.hotkeys.mapping
    svc.on_hotkey("ctrl+1")
    assert idle(svc) and typed(backend) == ["base"]
    svc.set_layer("nav")
    assert ("layer", "nav") in svc.events_seen
    assert "ctrl+2" in svc.hotkeys.mapping
    svc.on_hotkey("ctrl+1")  # the layered macro now wins over the base one
    assert idle(svc) and typed(backend) == ["base", "nav version"]
    svc.on_hotkey("ctrl+2")
    assert idle(svc) and typed(backend) == ["base", "nav version", "nav only"]
    svc.set_layer("")
    svc.on_hotkey("ctrl+1")
    assert idle(svc) and typed(backend)[-1] == "base"


def test_one_shot_layer_reverts_after_one_hotkey(svc, db, backend):
    db.save_macro(typing("base", hotkey="ctrl+1"))
    db.save_macro(typing("layered", hotkey="ctrl+1", layer="nav"))
    svc.reload()
    svc.set_layer("nav", one_shot=True)
    svc.on_hotkey("ctrl+1")
    assert idle(svc) and typed(backend) == ["layered"]
    assert svc.active_layer == ""
    svc.on_hotkey("ctrl+1")
    assert idle(svc) and typed(backend) == ["layered", "base"]


def test_switch_layer_step_and_sticky_layer(svc, db, backend):
    db.save_macro(typing("layered", hotkey="ctrl+1", layer="nav"))
    svc.run(Macro("to nav", [A("switch_layer", layer="nav")]), blocking=True)
    assert svc.active_layer == "nav"
    svc.on_hotkey("ctrl+1")  # a plain (not one-shot) layer stays active
    assert idle(svc)
    svc.on_hotkey("ctrl+1")
    assert idle(svc) and typed(backend) == ["layered", "layered"]
    svc.run(Macro("back", [A("switch_layer", layer="")]), blocking=True)
    assert svc.active_layer == "" and "ctrl+1" not in svc.hotkeys.mapping


def test_layer_does_not_gate_schedules(svc, db):
    from keyboardmanager.core.schedule import Schedule

    db.save_macro(typing("timed", layer="nav", schedule=Schedule("interval", minutes=5)))
    svc.reload()
    assert len(svc.scheduler.upcoming()) == 1  # layers only affect hotkeys / abbreviations


# ----------------------------------------------------------------- conflicts
def test_same_hotkey_on_another_layer_or_profile_is_allowed():
    from keyboardmanager.core.conflicts import hotkey_conflict

    base = typing("base", hotkey="ctrl+1", id=1)
    layered = typing("layered", hotkey="ctrl+1", layer="Nav", id=2)
    assert hotkey_conflict(layered, [base], {}) is None
    assert hotkey_conflict(typing("l2", hotkey="ctrl+1", layer="nav", id=3), [layered], {}) is not None
    work = typing("w", hotkey="ctrl+2", profiles=["Work"], id=4)
    game = typing("g", hotkey="ctrl+2", profiles=["Game"], id=5)
    assert hotkey_conflict(game, [work], {}) is None
    assert hotkey_conflict(typing("w2", hotkey="ctrl+2", profiles=["work", "X"], id=6), [work], {}) is not None
    everywhere = typing("e", hotkey="ctrl+2", id=7)
    assert hotkey_conflict(everywhere, [work], {}) is not None  # [] = active in every profile


# ------------------------------------------------------------- context rules
def test_rule_matching_and_description():
    rule = ContextRule(app="chrome, firefox", title="docs", profile="Web")
    assert rule.matches(ActiveWindow("Google Chrome", "My Docs - Edit"))
    assert not rule.matches(ActiveWindow("Google Chrome", "News"))
    assert not rule.matches(ActiveWindow("Terminal", "docs"))
    assert not rule.matches(None)
    assert ContextRule(title="draft", layer="write").matches(ActiveWindow("Anything", "a Draft"))
    assert 'profile "Web"' in rule.describe() and "chrome" in rule.describe()
    assert 'layer "base"' in ContextRule(app="x", layer="").describe()
    assert 'profile "All profiles"' in ContextRule(app="x", profile="").describe()


def test_rule_json_round_trip_keeps_none_and_empty_apart():
    rules = [ContextRule(app="code", profile="Work"),              # layer untouched
             ContextRule(title="game", profile="Game", layer=""),  # layer -> base
             ContextRule(app="x", layer="nav", enabled=False)]
    again = rules_from_json(rules_to_json(rules))
    assert again == rules
    assert again[0].layer is None and again[1].layer == "" and not again[2].enabled
    assert rules_from_json("") == []
    with pytest.raises(ValueError):
        rules_from_json('{"app": "x"}')
    with pytest.raises(ValueError):
        rules_from_json('[{"app": 3}]')


@pytest.mark.parametrize("rule, message", [
    (ContextRule(profile="Work"), "which windows"),
    (ContextRule(app="code"), "profile and/or the layer"),
    (ContextRule(app="code", profile="a,b"), "comma"),
])
def test_rule_validation(rule, message):
    with pytest.raises(ValueError, match=message):
        validate_rule(rule)


def test_watcher_applies_and_reverts():
    window = [ActiveWindow("Terminal", "bash")]
    applied = []
    watcher = ContextWatcher(active_window=lambda: window[0], apply=applied.append)
    specific = ContextRule(app="chrome", title="mail", profile="Mail")
    general = ContextRule(app="chrome", profile="Web")
    watcher.update([specific, general, ContextRule(app="term", profile="X", enabled=False)], thread=False)
    watcher.check()
    assert applied == []  # no rule matches (the disabled one never): nothing to do yet
    window[0] = ActiveWindow("Google Chrome", "News")
    watcher.check()
    watcher.check()  # unchanged window: applied once
    assert applied == [general]
    window[0] = ActiveWindow("Google Chrome", "Other page")
    watcher.check()
    assert applied == [general]  # another window, the same rule: not applied again
    window[0] = ActiveWindow("Google Chrome", "Mail - Inbox")
    watcher.check()
    assert applied[-1] is specific  # the first matching rule wins
    window[0] = ActiveWindow("Terminal", "bash")
    watcher.check()
    assert applied[-1] is None  # no match any more: hand control back
    assert len(applied) == 3


def test_watcher_update_reapplies_and_stop_reverts():
    window = [ActiveWindow("Code", "main.py")]
    applied = []
    watcher = ContextWatcher(active_window=lambda: window[0], apply=applied.append)
    first = ContextRule(app="code", layer="dev")
    watcher.update([first], thread=False)
    watcher.check()
    assert applied == [first]
    second = ContextRule(app="code", layer="dev2")
    watcher.update([second], thread=False)
    watcher.check()  # the same window, but the rules changed
    assert applied == [first, second]
    watcher.stop()
    assert applied == [first, second, None]  # switching off hands control back


def test_watcher_thread_runs_only_while_rules_exist(monkeypatch):
    monkeypatch.setattr(context_mod, "POLL_INTERVAL_S", 0.01)
    window = [ActiveWindow("Code", "x")]
    applied = []
    watcher = ContextWatcher(active_window=lambda: window[0], apply=applied.append, interval=0.01)
    watcher.update([ContextRule(app="code", layer="dev")])
    assert wait_until(lambda: applied)
    assert watcher._thread is not None
    watcher.update([])
    assert watcher._thread is None


# -------------------------------------------------- context rules in the service
def test_context_rules_switch_profile_and_layer(svc, db, backend):
    db.save_macro(typing("work", hotkey="ctrl+1", profiles=["Work"]))
    db.save_macro(typing("nav", hotkey="ctrl+2", layer="nav"))
    svc.set_context_rules([ContextRule(app="code", profile="Work", layer="nav")])
    svc.set_active_profile("Home")
    backend.window = ActiveWindow("Terminal", "bash")
    svc.contexts.check()
    assert svc.active_profile == "Home" and svc.active_layer == ""
    assert "ctrl+1" not in svc.hotkeys.mapping and "ctrl+2" not in svc.hotkeys.mapping
    backend.window = ActiveWindow("VS Code", "main.py")
    svc.contexts.check()
    assert svc.active_profile == "Work" and svc.active_layer == "nav"
    assert ("profile", "Work") in svc.events_seen and ("layer", "nav") in svc.events_seen
    assert {"ctrl+1", "ctrl+2"} <= set(svc.hotkeys.mapping)
    assert svc.chosen_profile == "Home"  # the hand-made choice is untouched
    backend.window = ActiveWindow("Terminal", "bash")
    svc.contexts.check()
    assert svc.active_profile == "Home" and svc.active_layer == ""
    assert "ctrl+1" not in svc.hotkeys.mapping and "ctrl+2" not in svc.hotkeys.mapping


def test_context_rule_returns_to_the_chosen_profile(svc, backend):
    svc.set_active_profile("Home")
    svc.set_context_rules([ContextRule(app="game", profile="Gaming")])
    backend.window = ActiveWindow("Game", "Level 1")
    svc.contexts.check()
    assert svc.active_profile == "Gaming"
    backend.window = ActiveWindow("Mail", "Inbox")
    svc.contexts.check()
    assert svc.active_profile == "Home"


def test_manual_choice_clears_the_context_override(svc, backend):
    svc.set_context_rules([ContextRule(app="game", profile="Gaming", layer="g")])
    backend.window = ActiveWindow("Game", "")
    svc.contexts.check()
    assert svc.active_profile == "Gaming" and svc.active_layer == "g"
    svc.set_active_profile("Work")
    assert svc.active_profile == "Work"
    svc.set_layer("other")
    assert svc.active_layer == "other"


def test_contexts_can_be_switched_off(svc, backend):
    svc.set_context_rules([ContextRule(app="game", profile="Gaming")])
    assert svc.contexts_enabled
    svc.set_contexts_enabled(False)
    backend.window = ActiveWindow("Game", "")
    svc.contexts.check()  # no rules are loaded while disabled
    assert svc.active_profile == ""
    svc.set_contexts_enabled(True)
    svc.contexts.check()
    assert svc.active_profile == "Gaming"
    svc.set_contexts_enabled(False)  # switching off reverts the override
    assert svc.active_profile == ""


def test_invalid_context_rules_are_refused_and_bad_json_ignored(svc, db):
    with pytest.raises(ValueError):
        svc.set_context_rules([ContextRule(app="x")])
    db.set_setting("context_rules", "{broken")
    assert svc.context_rules == []


def test_profiles_and_layer_survive_in_macro_files():
    macro = typing("m", profiles=["Work"], layer="nav")
    data = macro.to_dict()
    assert data["profiles"] == ["Work"] and data["layer"] == "nav"
    again = Macro.from_dict(data)
    assert again.profiles == ["Work"] and again.layer == "nav"
    plain = typing("p").to_dict()
    assert "profiles" not in plain and "layer" not in plain
    with pytest.raises(ValueError, match="profiles"):
        Macro.from_dict({**data, "profiles": "Work"})
    with pytest.raises(ValueError, match="layer"):
        Macro.from_dict({**data, "layer": 3})
