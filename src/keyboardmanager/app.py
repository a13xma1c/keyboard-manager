"""Application entry point.

    keyboardmanager                    the application (window + tray icon)
    keyboardmanager --background       start with the window hidden (tray icon only)
    keyboardmanager --headless         no GUI at all: hotkeys, schedules, triggers,
                                       sync and remote control keep working
    keyboardmanager ctl COMMAND        control the running instance (remote control)

Only one instance runs per user and database: starting again shows the running
one's window instead.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from typing import Callable

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from keyboardmanager import __version__
from keyboardmanager.core.database import Database, default_db_path


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "ctl":
        return ctl_main(argv[1:])
    parser = argparse.ArgumentParser(prog="keyboardmanager", description="Keyboard shortcut and macro manager")
    parser.add_argument("--version", action="version", version=f"Keyboard Manager {__version__}")
    parser.add_argument("--db", help="path of the SQLite database (default: per-user data directory)")
    parser.add_argument(
        "--hotkey-backend", choices=("auto", "pynput", "keyboard"), default="auto",
        help="library used for global hotkeys",
    )
    parser.add_argument("--background", action="store_true",
                        help="start with the window hidden (tray icon only), e.g. at login")
    parser.add_argument("--headless", action="store_true",
                        help="run without any window (a background service); stop with Ctrl+C")
    args, qt_args = parser.parse_known_args(argv)
    db_path = str(args.db or default_db_path())
    if args.headless:
        return run_headless(db_path, args.hotkey_backend)

    from keyboardmanager.gui.instance import SingleInstance, server_name

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Keyboard Manager")
    app.setQuitOnLastWindowClosed(False)

    instance = SingleInstance(server_name(db_path))
    if instance.notify_running("show"):
        print("Keyboard Manager is already running: its window was brought to the front.", file=sys.stderr)
        return 0
    instance.listen()

    try:
        from keyboardmanager.core.backend import PynputBackend
        from keyboardmanager.core.hotkeys import create_hotkey_backend
        from keyboardmanager.core.service import MacroService
        from keyboardmanager.gui.mainwindow import MainWindow

        db = Database(db_path)
        service = MacroService(db, PynputBackend(), create_hotkey_backend(args.hotkey_backend))
        window = MainWindow(db, service)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, "Keyboard Manager", f"Failed to start: {exc}")
        return 1

    instance.message.connect(lambda message: window._show_window() if message == "show" else None)
    # Qt: "connect clean-up code to aboutToQuit ... on some platforms the exec() call
    # may not return" - on macOS quitting ends the process from inside the event loop.
    app.aboutToQuit.connect(service.shutdown)
    service.apply_remote()
    service.start_auto_sync()
    if not (args.background and QSystemTrayIcon.isSystemTrayAvailable()):
        window.show()
    code = app.exec()
    service.shutdown()  # where exec() does return; shutdown is idempotent
    instance.close()
    db.close()
    return code


# ---------------------------------------------------------------- headless
def run_headless(db_path: str, hotkey_backend: str = "auto", backend=None, hotkeys=None,
                 stop_after_ms: int | None = None, out: Callable[[str], None] = print) -> int:
    """The service without a GUI: until Ctrl+C / SIGTERM (``stop_after_ms`` in tests).

    Macros that need the GUI do not work here: questions (Ask for input) fail, the
    clipboard is private to the process, and the vision steps cannot see the screen.
    """
    from PySide6.QtCore import QCoreApplication, QTimer

    from keyboardmanager.core.service import MacroService
    from keyboardmanager.gui.instance import SingleInstance, server_name

    app = QCoreApplication.instance() or QCoreApplication([sys.argv[0]])
    instance = SingleInstance(server_name(db_path))
    if instance.notify_running("status"):
        out("Keyboard Manager is already running for this database.")
        return 1
    instance.listen()
    if backend is None:
        from keyboardmanager.core.backend import PynputBackend

        backend = PynputBackend()
    if hotkeys is None:
        from keyboardmanager.core.hotkeys import create_hotkey_backend

        hotkeys = create_hotkey_backend(hotkey_backend)
    db = Database(db_path)
    service = MacroService(db, backend, hotkeys)
    try:
        failures = service.reload()
        service.apply_remaps()
        service.fire_startup_event()
        remote_error = service.apply_remote()
        service.start_auto_sync()
        active = sum(1 for m in db.list_macros() if service.macro_active(m) and m.hotkey)
        out(f"Keyboard Manager {__version__} running headless ({db_path})")
        out(f"  hotkeys: {active} active, stop = {service.stop_hotkey}"
            + (f", {len(failures)} problem(s)" if failures else ""))
        if service.remote is not None:
            out(f"  remote control: http://{service.remote.host}:{service.remote.port}/ "
                "(token: see the Sync & Remote tab, or 'keyboardmanager ctl url')")
        else:
            out("  remote control: " + (remote_error or "off"))
        if service.sync_folder or service.teams:
            out(f"  sync: every {service.sync_interval} min" if service.sync_interval else "  sync: on changes")
        out("Stop with Ctrl+C.")

        def quit_(*_args) -> None:
            app.quit()

        previous = {sig: signal.signal(sig, quit_) for sig in (signal.SIGINT, signal.SIGTERM)}
        ticker = QTimer()  # wakes the event loop so Python can run the signal handlers
        ticker.timeout.connect(lambda: None)
        ticker.start(250)
        if stop_after_ms is not None:
            QTimer.singleShot(stop_after_ms, app.quit)
        app.exec()
        ticker.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        out("Stopped.")
        return 0
    finally:
        service.shutdown()
        instance.close()
        db.close()


# --------------------------------------------------------------------- ctl
def ctl_main(argv: list[str], out: Callable[[str], None] = print,
             err: Callable[[str], None] = lambda text: print(text, file=sys.stderr)) -> int:
    """``keyboardmanager ctl ...``: talk to the running instance's remote control."""
    from keyboardmanager.core.remote import RemoteClient, RemoteError

    parser = argparse.ArgumentParser(prog="keyboardmanager ctl",
                                     description="Control the running Keyboard Manager (needs remote control on)")
    parser.add_argument("--db", help="database of the instance (default: the usual one)")
    parser.add_argument("--json", action="store_true", help="print the raw JSON answer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="what is running, the queue, profile and layer")
    sub.add_parser("list", help="the macros that can be run")
    history = sub.add_parser("history", help="recent runs")
    history.add_argument("--limit", type=int, default=10)
    run = sub.add_parser("run", help="run a macro (by name or id)")
    run.add_argument("macro")
    run.add_argument("--queue", action="store_true", help="queue it when another macro is running")
    for name in ("stop", "pause", "resume"):
        sub.add_parser(name, help=f"{name} the running macro")
    profile = sub.add_parser("profile", help='switch the profile ("" = All profiles)')
    profile.add_argument("name")
    layer = sub.add_parser("layer", help="switch the keymap layer (no name = base)")
    layer.add_argument("name", nargs="?", default="")
    layer.add_argument("--one-shot", action="store_true")
    sub.add_parser("sync", help="sync now (folder and team libraries)")
    sub.add_parser("url", help="print the address of the web panel")
    args = parser.parse_args(argv)

    path = str(args.db or default_db_path())
    if not os.path.exists(path):
        err(f"No Keyboard Manager database at {path}.")
        return 2
    db = Database(path)
    try:
        enabled = db.get_setting("remote_enabled", "0") == "1"
        port = int(db.get_setting("remote_port", "8765") or 8765)
        token = db.get_setting("remote_token", "")
    finally:
        db.close()
    if not enabled or not token:
        err("Remote control is off. Turn it on in Keyboard Manager: Sync & Remote > Remote control.")
        return 2
    if args.command == "url":
        out(f"http://127.0.0.1:{port}/?token={token}")
        return 0
    client = RemoteClient(port, token)
    try:
        result = _ctl_call(client, args)
    except RemoteError as exc:
        err(f"Error: {exc}")
        return 1
    if args.json:
        out(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        out(_ctl_text(args.command, result))
    return 0


def _ctl_call(client, args):
    c = args.command
    if c == "status":
        return client.call("GET", "/api/status")
    if c == "list":
        return client.call("GET", "/api/macros")
    if c == "history":
        return client.call("GET", f"/api/history?limit={args.limit}")
    if c == "run":
        return client.call("POST", "/api/run", {"macro": args.macro, "queue": args.queue})
    if c in ("stop", "pause", "resume"):
        return client.call("POST", f"/api/{c}")
    if c == "profile":
        return client.call("POST", "/api/profile", {"name": args.name})
    if c == "layer":
        return client.call("POST", "/api/layer", {"name": args.name, "one_shot": args.one_shot})
    return client.call("POST", "/api/sync")


def _ctl_text(command: str, r) -> str:
    if command == "status":
        lines = [f'Running: {r["running"]}' + (" (paused)" if r["paused"] else "") if r["running"] else "Idle"]
        if r["queue"]:
            lines.append("Queue: " + ", ".join(r["queue"]))
        lines.append(f'Profile: {r["profile"] or "All profiles"}')
        lines.append(f'Layer: {r["layer"] or "base"}')
        return "\n".join(lines)
    if command == "list":
        return "\n".join(
            ("\u2605 " if m["favorite"] else "  ") + m["name"]
            + (f'  [{m["hotkey"]}]' if m["hotkey"] else "")
            + ("" if m["enabled"] and m["active"] else "  (inactive)")
            for m in r) or "(no macros)"
    if command == "history":
        return "\n".join(f'{h["started_at"]}  {h["macro"]}  {h["status"]}  ({h["trigger"]})' for h in r) \
            or "(no runs yet)"
    if command == "run":
        return f'{"Queued" if r["queued"] else "Started"} "{r["macro"]}"'
    if command == "profile":
        return f'Profile: {r["profile"] or "All profiles"}'
    if command == "layer":
        return f'Layer: {r["layer"] or "base"}'
    if command == "sync":
        return r["summary"]
    return "OK" if r.get("changed", True) else "Nothing to do"


if __name__ == "__main__":
    sys.exit(main())
