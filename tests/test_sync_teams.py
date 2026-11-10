"""Sync between computers, team libraries and share codes (two databases, real folders)."""

import json

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import share
from keyboardmanager.core import sync as sync_mod
from keyboardmanager.core import team as team_mod
from keyboardmanager.core.context import ContextRule
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.core.sync import SyncError, macro_hash, sync_folder
from keyboardmanager.core.team import TeamConflict, TeamError, TeamLibrary
from keyboardmanager.core.triggers import TriggerRule
from tests_support import FakeBackend, FakeHotkeys, wait_until


def A(t, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    return a


def typing(name, text=None, **kw):
    return Macro(name, [A("type_text", text=text or name)], **kw)


@pytest.fixture
def two(tmp_path):
    """Two computers' databases and a shared folder."""
    laptop, desktop = Database(":memory:"), Database(":memory:")
    folder = tmp_path / "Dropbox"
    folder.mkdir()
    yield laptop, desktop, folder
    laptop.close()
    desktop.close()


def names(db):
    return sorted(m.name for m in db.list_macros())


def sync(db, folder, device):
    return sync_folder(db, folder, device)


# ------------------------------------------------------------------- basics
def test_new_macros_travel_both_ways(two):
    laptop, desktop, folder = two
    laptop.save_macro(typing("from laptop", hotkey="ctrl+1"))
    desktop.save_macro(typing("from desktop"))
    r1 = sync(laptop, folder, "laptop")
    assert r1.uploaded == ["from laptop"] and r1.downloaded == []
    r2 = sync(desktop, folder, "desktop")
    assert r2.uploaded == ["from desktop"] and r2.downloaded == ["from laptop"]
    r3 = sync(laptop, folder, "laptop")
    assert r3.downloaded == ["from desktop"]
    assert names(laptop) == names(desktop) == ["from desktop", "from laptop"]
    # the same identity on both computers
    assert {m.uid for m in laptop.list_macros()} == {m.uid for m in desktop.list_macros()}
    assert desktop.get_macro_by_uid(laptop.list_macros()[1].uid).hotkey == "ctrl+1"
    again = sync(laptop, folder, "laptop")
    assert again.summary() == "everything was up to date"


def test_edits_and_deletions_travel(two):
    laptop, desktop, folder = two
    m = laptop.save_macro(typing("greet", "hello"))
    sync(laptop, folder, "laptop")
    sync(desktop, folder, "desktop")
    m.actions[0].params["text"] = "hello there"
    laptop.save_macro(m)
    assert sync(laptop, folder, "laptop").uploaded == ["greet"]
    assert sync(desktop, folder, "desktop").downloaded == ["greet"]
    assert desktop.get_macro_by_uid(m.uid).actions[0].params["text"] == "hello there"
    desktop.delete_macro(desktop.get_macro_by_uid(m.uid).id)
    assert sync(desktop, folder, "desktop").deleted_there == ["greet"]
    assert sync(laptop, folder, "laptop").deleted_here == ["greet"]
    assert names(laptop) == names(desktop) == []
    entry = json.loads((folder / "keyboardmanager-sync" / "macros" / f"{m.uid}.json").read_text())
    assert entry["deleted"] is True and entry["device"] == "desktop"  # a tombstone


def test_conflict_keeps_both_versions(two):
    laptop, desktop, folder = two
    m = laptop.save_macro(typing("greet", "hello"))
    sync(laptop, folder, "laptop")
    sync(desktop, folder, "desktop")
    m.actions[0].params["text"] = "laptop version"
    laptop.save_macro(m)
    d = desktop.get_macro_by_uid(m.uid)
    d.actions[0].params["text"] = "desktop version"
    desktop.save_macro(d)
    sync(laptop, folder, "laptop")
    report = sync(desktop, folder, "desktop")
    assert report.conflicts == ["greet"]
    texts = {x.name: x.actions[0].params["text"] for x in desktop.list_macros()}
    assert texts == {"greet": "desktop version", "greet (conflict from laptop)": "laptop version"}
    sync(laptop, folder, "laptop")  # the desktop's version won in the folder; the copy travels too
    texts = {x.name: x.actions[0].params["text"] for x in laptop.list_macros()}
    assert texts == {"greet": "desktop version", "greet (conflict from laptop)": "laptop version"}


def test_change_beats_deletion(two):
    laptop, desktop, folder = two
    m = laptop.save_macro(typing("keep me"))
    sync(laptop, folder, "laptop")
    sync(desktop, folder, "desktop")
    laptop.delete_macro(m.id)
    d = desktop.get_macro_by_uid(m.uid)
    d.description = "edited meanwhile"
    desktop.save_macro(d)
    sync(laptop, folder, "laptop")   # tombstone written
    sync(desktop, folder, "desktop")  # changed here, deleted there: the change wins
    sync(laptop, folder, "laptop")
    assert laptop.get_macro_by_uid(m.uid).description == "edited meanwhile"


def test_change_there_beats_deletion_here(two):
    laptop, desktop, folder = two
    m = laptop.save_macro(typing("keep me"))
    sync(laptop, folder, "laptop")
    sync(desktop, folder, "desktop")
    d = desktop.get_macro_by_uid(m.uid)
    d.description = "edited on the desktop"
    desktop.save_macro(d)
    sync(desktop, folder, "desktop")  # the change is in the folder first
    laptop.delete_macro(m.id)
    report = sync(laptop, folder, "laptop")  # deleted here, changed there: restored
    assert report.downloaded == ["keep me"] and report.deleted_there == []
    assert laptop.get_macro_by_uid(m.uid).description == "edited on the desktop"


def test_missing_files_never_delete_anything(two):
    laptop, _, folder = two
    m = laptop.save_macro(typing("precious"))
    sync(laptop, folder, "laptop")
    (folder / "keyboardmanager-sync" / "macros" / f"{m.uid}.json").unlink()  # e.g. still downloading
    report = sync(laptop, folder, "laptop")
    assert names(laptop) == ["precious"] and report.uploaded == ["precious"]


def test_missing_folder_is_an_error_not_a_wipe(two, tmp_path):
    laptop, _, _ = two
    laptop.save_macro(typing("safe"))
    with pytest.raises(SyncError, match="does not exist"):
        sync(laptop, tmp_path / "unplugged", "laptop")
    with pytest.raises(SyncError):
        sync(laptop, "", "laptop")
    assert names(laptop) == ["safe"]


def test_unreadable_entries_are_skipped_and_reported(two):
    laptop, desktop, folder = two
    m = laptop.save_macro(typing("half"))
    sync(laptop, folder, "laptop")
    path = folder / "keyboardmanager-sync" / "macros" / f"{m.uid}.json"
    path.write_text("{ half a file")
    report = sync(desktop, folder, "desktop")
    assert names(desktop) == [] and report.errors and "half" not in report.downloaded
    report = sync(laptop, folder, "laptop")  # the broken file is left for the sync client to fix
    assert len(report.errors) == 1 and report.uploaded == []
    assert path.read_text() == "{ half a file"


def test_incoming_names_and_hotkeys_never_clash(two):
    laptop, desktop, folder = two
    laptop.save_macro(typing("Sign", hotkey="ctrl+1"))
    desktop.save_macro(typing("Sign", "other", hotkey="ctrl+1"))  # made separately
    sync(laptop, folder, "laptop")
    report = sync(desktop, folder, "desktop")
    assert names(desktop) == ["Sign", "Sign (2)"] and len(report.adjusted) == 1
    assert sorted(m.hotkey for m in desktop.list_macros()) == ["", "ctrl+1"]
    sync(laptop, folder, "laptop")
    # both computers settled the clash the same way (the smaller uid keeps name and hotkey)
    state = lambda db: sorted((m.uid, m.name, m.hotkey) for m in db.list_macros())  # noqa: E731
    assert state(laptop) == state(desktop)
    winner = min(desktop.list_macros(), key=lambda m: m.uid)
    assert (winner.name, winner.hotkey) == ("Sign", "ctrl+1")
    assert sync(desktop, folder, "desktop").summary() == "everything was up to date"
    assert sync(laptop, folder, "laptop").summary() == "everything was up to date"


def test_reserved_hotkeys_are_dropped_from_incoming_macros(two):
    laptop, desktop, folder = two
    laptop.save_macro(typing("x", hotkey="ctrl+alt+f12"))
    sync(laptop, folder, "laptop")
    sync_folder(desktop, folder, "desktop", reserved={"ctrl+alt+f12": "stop"})
    assert desktop.list_macros()[0].hotkey == ""


def test_another_folder_starts_fresh(two, tmp_path):
    laptop, _, folder = two
    laptop.save_macro(typing("a"))
    sync(laptop, folder, "laptop")
    other = tmp_path / "Other"
    other.mkdir()
    assert sync(laptop, other, "laptop").uploaded == ["a"]
    assert sync_mod.scope_for(folder) != sync_mod.scope_for(other)


def test_devices_are_listed(two):
    laptop, desktop, folder = two
    sync(laptop, folder, "laptop")
    sync(desktop, folder, "desktop 2")
    devices = sync_mod.SyncFolder(folder).devices()
    assert sorted(d["device"] for d in devices) == ["desktop 2", "laptop"]


def test_macro_hash_ignores_local_details():
    a = typing("x")
    b = typing("x", id=5, uid="abc", source="", created_at="t")
    assert macro_hash(a) == macro_hash(b)
    b.description = "changed"
    assert macro_hash(a) != macro_hash(b)


def test_newer_format_is_not_touched(two):
    laptop, desktop, folder = two
    m = laptop.save_macro(typing("future"))
    sync(laptop, folder, "laptop")
    path = folder / "keyboardmanager-sync" / "macros" / f"{m.uid}.json"
    entry = json.loads(path.read_text())
    entry["version"] = 99
    path.write_text(json.dumps(entry))
    report = sync(desktop, folder, "desktop")
    assert names(desktop) == [] and "newer version" in report.errors[0]


# --------------------------------------------------- settings through the service
@pytest.fixture
def services(tmp_path):
    folder = tmp_path / "Shared"
    folder.mkdir()
    made = []
    for device in ("laptop", "desktop"):
        db = Database(":memory:")
        svc = MacroService(db, FakeBackend(), FakeHotkeys(), scheduler_thread=False)
        svc.set_sync_options(str(folder), device)
        made.append(svc)
    yield made
    for svc in made:
        svc.shutdown()
        svc.db.close()


def test_service_syncs_profiles_context_rules_and_triggers(services):
    laptop, desktop = services
    laptop.profiles.add("Work")
    laptop.set_context_rules([ContextRule(app="code", profile="Work")])
    laptop.db.save_macro(typing("Build"))
    laptop.save_trigger(TriggerRule("after", "clipboard", "Build", {"contains": "x"}))
    outcome = laptop.sync_now()
    assert not outcome.failed and outcome.report.settings == "uploaded"
    outcome = desktop.sync_now()
    assert outcome.report.settings == "downloaded" and outcome.changed_here
    assert desktop.all_profiles() == ["Work"]
    assert desktop.context_rules == [ContextRule(app="code", profile="Work")]
    assert [r.name for r in desktop.db.list_triggers()] == ["after"]
    assert laptop.sync_now().summary() == "Sync: everything was up to date"
    assert desktop.last_sync()["summary"].startswith("Sync:")


def test_settings_changed_on_both_computers_are_merged(services):
    laptop, desktop = services
    laptop.profiles.add("Work")
    laptop.sync_now()
    desktop.sync_now()
    laptop.profiles.add("Laptop only")
    laptop.save_trigger(TriggerRule("same name", "clipboard", "A"))
    desktop.profiles.add("Desktop only")
    desktop.save_trigger(TriggerRule("same name", "clipboard", "B"))
    laptop.sync_now()
    outcome = desktop.sync_now()
    assert outcome.report.settings == "merged"
    assert desktop.all_profiles() == ["Desktop only", "Laptop only", "Work"]
    assert [r.macro for r in desktop.db.list_triggers()] == ["A"]  # the folder's wins on a name clash
    laptop.sync_now()
    assert laptop.all_profiles() == desktop.all_profiles()
    assert laptop.sync_now().report.settings == "" and desktop.sync_now().report.settings == ""


def test_settings_sync_can_be_switched_off(services):
    laptop, desktop = services
    laptop.set_sync_options(laptop.sync_folder, "laptop", settings_too=False)
    laptop.profiles.add("Private")
    laptop.sync_now()
    desktop.sync_now()
    assert desktop.all_profiles() == []


def test_sync_options_validate(services, tmp_path):
    laptop, _ = services
    with pytest.raises(ValueError, match="does not exist"):
        laptop.set_sync_options(str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="interval"):
        laptop.set_sync_options("", interval=-1)
    laptop.set_sync_options("", "")
    assert laptop.sync_folder == "" and laptop.device_name  # the computer's name by default
    assert laptop.sync_now().summary().startswith("Nothing to sync")


def test_sync_failure_is_reported(services, tmp_path):
    laptop, _ = services
    laptop.db.set_setting(sync_mod.FOLDER_SETTING, str(tmp_path / "gone"))
    outcome = laptop.sync_now()
    assert outcome.failed and "does not exist" in outcome.summary()
    assert laptop.last_sync()["failed"] is True


def test_auto_sync_thread_and_requests(services, monkeypatch):
    from keyboardmanager.core import service as service_mod

    monkeypatch.setattr(service_mod, "SYNC_DEBOUNCE_S", 0.01)
    laptop, desktop = services
    events = []
    laptop.listener = lambda e, p: events.append(e)
    laptop.db.save_macro(typing("auto"))
    laptop.start_auto_sync()
    assert wait_until(lambda: "sync" in events)  # syncs once at start
    events.clear()
    laptop.db.save_macro(typing("second"))
    laptop.request_sync()
    assert wait_until(lambda: "sync" in events)
    desktop.sync_now()
    assert names(desktop.db) == ["auto", "second"]


# ------------------------------------------------------------------- teams
@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "TeamShare"
    folder.mkdir()
    TeamLibrary.create(folder, "Support")
    return folder


def sub_for(db, folder):
    return team_mod.new_subscription(str(folder))


def test_create_and_open_a_library(tmp_path, library):
    assert TeamLibrary(library).name == "Support"
    with pytest.raises(TeamError, match="already"):
        TeamLibrary.create(library, "Again")
    with pytest.raises(TeamError, match="not a team library"):
        TeamLibrary(tmp_path)
    with pytest.raises(TeamError, match="does not exist"):
        TeamLibrary.create(tmp_path / "missing", "X")
    with pytest.raises(TeamError, match="name"):
        TeamLibrary.create(tmp_path, "  ")


def test_publish_pull_update_and_remove(two, library):
    alice, bob, _ = two
    a_sub, b_sub = sub_for(alice, library), sub_for(bob, library)
    m = alice.save_macro(typing("Reply template", "Thanks for contacting us", hotkey="ctrl+1",
                                folder="Mine", favorite=True))
    assert team_mod.publish(alice, a_sub, m, "Alice") == 1
    report = team_mod.pull(bob, b_sub)
    assert report.added == ["Reply template"]
    copy = bob.get_macro_by_uid(m.uid)
    assert copy.source == b_sub.source and copy.folder == "Team/Support" and not copy.favorite
    assert copy.hotkey == "ctrl+1" and "Published by Alice." in copy.description
    assert team_mod.pull(bob, b_sub).summary() == "up to date"
    # Alice edits her own macro and publishes again
    m.actions[0].params["text"] = "Thanks!"
    alice.save_macro(m)
    assert team_mod.publish(alice, a_sub, m, "Alice") == 2
    copy.enabled = False  # Bob switched it off locally
    bob.save_macro(copy)
    report = team_mod.pull(bob, b_sub)
    assert report.updated == ["Reply template"]
    copy = bob.get_macro_by_uid(m.uid)
    assert copy.actions[0].params["text"] == "Thanks!" and not copy.enabled  # his choice is kept
    assert copy.description.count("Published by") == 1
    team_mod.unpublish(alice, a_sub, m.uid)
    assert team_mod.pull(bob, b_sub).removed == ["Reply template"]
    assert bob.get_macro_by_uid(m.uid) is None
    assert alice.get_macro(m.id) is not None  # the author keeps her macro


def test_publishing_over_someone_elses_change_is_refused(two, library):
    alice, bob, _ = two
    a_sub, b_sub = sub_for(alice, library), sub_for(bob, library)
    m = alice.save_macro(typing("Shared"))
    team_mod.publish(alice, a_sub, m, "Alice")
    team_mod.pull(bob, b_sub)
    mine = team_mod.detach(bob, bob.get_macro_by_uid(m.uid))  # Bob takes it over...
    mine.description = "Bob's improvement"
    bob.save_macro(mine)
    assert team_mod.publish(bob, b_sub, mine, "Bob") == 2       # ...and publishes version 2
    m.description = "Alice's change"
    with pytest.raises(TeamConflict, match="changed in the library by Bob"):
        team_mod.publish(alice, a_sub, m, "Alice")             # she has not seen version 2
    assert team_mod.publish(alice, a_sub, m, "Alice", force=True) == 3


def test_own_macros_are_not_copied_and_team_macros_cannot_be_published(two, library):
    alice, bob, _ = two
    a_sub, b_sub = sub_for(alice, library), sub_for(bob, library)
    m = alice.save_macro(typing("Mine"))
    team_mod.publish(alice, a_sub, m, "Alice")
    assert team_mod.pull(alice, a_sub).summary() == "up to date"  # her own: no copy
    assert names(alice) == ["Mine"]
    team_mod.pull(bob, b_sub)
    with pytest.raises(TeamError, match="team macro"):
        team_mod.publish(bob, b_sub, bob.get_macro_by_uid(m.uid), "Bob")
    with pytest.raises(TeamError, match="save the macro first"):
        team_mod.publish(bob, b_sub, typing("unsaved"), "Bob")


def test_pulled_macros_never_take_your_names_or_hotkeys(two, library):
    alice, bob, _ = two
    a_sub, b_sub = sub_for(alice, library), sub_for(bob, library)
    bob.save_macro(typing("Greeting", hotkey="ctrl+1"))
    team_mod.publish(alice, a_sub, alice.save_macro(typing("Greeting", hotkey="ctrl+1")), "Alice")
    report = team_mod.pull(bob, b_sub)
    assert report.added == ["Greeting (Support)"] and report.cleared == ["Greeting (Support)"]
    assert bob.get_macro_by_uid(alice.list_macros()[0].uid).hotkey == ""


def test_unsubscribe_removes_the_copies(two, library):
    alice, bob, _ = two
    a_sub, b_sub = sub_for(alice, library), sub_for(bob, library)
    team_mod.publish(alice, a_sub, alice.save_macro(typing("T")), "Alice")
    team_mod.pull(bob, b_sub)
    bob.save_macro(typing("own"))
    assert team_mod.unsubscribe(bob, b_sub) == 1
    assert names(bob) == ["own"] and bob.sync_all(b_sub.scope) == {}


def test_damaged_team_entries_are_reported(two, library):
    alice, bob, _ = two
    (library / "keyboardmanager-team" / "macros" / "broken.json").write_text("nope")
    report = team_mod.pull(bob, sub_for(bob, library))
    assert report.errors and report.added == []


def test_team_macros_are_not_synced_personally(two, library, tmp_path):
    alice, bob, folder = two
    team_mod.publish(alice, sub_for(alice, library), alice.save_macro(typing("Team thing")), "Alice")
    team_mod.pull(bob, sub_for(bob, library))
    report = sync(bob, folder, "bob")
    assert report.uploaded == []


def test_service_team_workflow(tmp_path):
    folder = tmp_path / "T"
    folder.mkdir()
    a = MacroService(Database(":memory:"), FakeBackend(), FakeHotkeys(), scheduler_thread=False)
    b = MacroService(Database(":memory:"), FakeBackend(), FakeHotkeys(), scheduler_thread=False)
    try:
        a.set_author("Alice")
        sub = a.create_team(str(folder), "Ops")
        assert [t.name for t in a.teams] == ["Ops"]
        with pytest.raises(TeamError, match="already use"):
            a.subscribe_team(str(folder))
        m = a.db.save_macro(typing("Deploy", hotkey="ctrl+d"))
        assert a.publish([m.id], sub.id) == ['"Deploy": published (version 1)']
        report = b.subscribe_team(str(folder))
        assert report.added == ["Deploy"]
        copy = b.db.list_macros()[0]
        assert b.team_of(copy).name == "Ops" and "ctrl+d" in b.hotkeys.mapping  # team hotkeys work
        b.detach(copy.id)
        assert b.db.get_macro(copy.id).source == "" and b.team_of(b.db.get_macro(copy.id)) is None
        b_sub = b.teams[0]
        assert b.unsubscribe_team(b_sub.id) == 0  # detached: nothing left to remove
        a.unpublish(m.id, sub.id)
        assert not (folder / "keyboardmanager-team" / "macros" / f"{m.uid}.json").exists()
        outcome = a.sync_now()
        assert outcome.teams["Ops"].summary() == "up to date"
    finally:
        for svc in (a, b):
            svc.shutdown()
            svc.db.close()


def test_team_errors_are_reported_by_sync_now(tmp_path):
    folder = tmp_path / "T"
    folder.mkdir()
    svc = MacroService(Database(":memory:"), FakeBackend(), FakeHotkeys(), scheduler_thread=False)
    try:
        sub = svc.create_team(str(folder), "Gone soon")
        (folder / "keyboardmanager-team" / "team.json").unlink()
        outcome = svc.sync_now()
        assert outcome.failed and "Gone soon" in outcome.team_errors
        with pytest.raises(TeamError):
            svc.team("nope")
        assert svc.publish([999], sub.id) == []
    finally:
        svc.shutdown()
        svc.db.close()


# ------------------------------------------------------------- share codes
def test_share_code_round_trip(two):
    laptop, desktop, _ = two
    m = laptop.save_macro(typing("Signature", "Best regards", hotkey="ctrl+s"))
    code = share.encode([m])
    assert code.startswith("KMSHARE1:") and "\n" not in code
    wrapped = "\n".join(code[i:i + 40] for i in range(0, len(code), 40))  # as a mail client would
    assert share.looks_like_code(wrapped)
    from keyboardmanager.core.io import import_from_text

    result = import_from_text(share.decode(wrapped), desktop)
    assert [x.name for x in result.imported] == ["Signature"]
    assert desktop.list_macros()[0].actions[0].params["text"] == "Best regards"
    assert desktop.list_macros()[0].uid != m.uid  # a copy, not the same macro


@pytest.mark.parametrize("bad, message", [
    ("hello", "not a Keyboard Manager share code"),
    ("KMSHARE1:!!!!", "not base64"),
    ("KMSHARE1:" + "A" * 10, "damaged"),
])
def test_bad_share_codes(bad, message):
    with pytest.raises(share.ShareError, match=message):
        share.decode(bad)


def test_truncated_and_oversized_share_codes(monkeypatch):
    code = share.encode([typing("x" * 50)])
    with pytest.raises(share.ShareError, match="incomplete|damaged"):
        share.decode(code[: len(code) // 2])
    monkeypatch.setattr(share, "MAX_DOCUMENT_BYTES", 20)
    with pytest.raises(share.ShareError, match="too much"):
        share.decode(code)
    monkeypatch.setattr(share, "MAX_CODE_CHARS", 10)
    with pytest.raises(share.ShareError, match="too long"):
        share.decode(code)
    with pytest.raises(share.ShareError, match="at least one"):
        share.encode([])


def test_service_share_code(tmp_path):
    a = MacroService(Database(":memory:"), FakeBackend(), FakeHotkeys(), scheduler_thread=False)
    b = MacroService(Database(":memory:"), FakeBackend(), FakeHotkeys(), scheduler_thread=False)
    try:
        m = a.db.save_macro(typing("Shared", hotkey="ctrl+1"))
        b.db.save_macro(typing("Shared", hotkey="ctrl+1"))
        result = b.import_share_code(a.share_code([m.id]))
        assert result.renamed == 1 and result.hotkeys_cleared == 1
        assert names(b.db) == ["Shared", "Shared (2)"]
    finally:
        for svc in (a, b):
            svc.shutdown()
            svc.db.close()
