"""Application entry point."""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from keyboardmanager.core.database import Database, default_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keyboardmanager", description="Keyboard shortcut and macro manager")
    parser.add_argument("--db", help="path of the SQLite database (default: per-user data directory)")
    parser.add_argument(
        "--hotkey-backend", choices=("auto", "pynput", "keyboard"), default="auto",
        help="library used for global hotkeys",
    )
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Keyboard Manager")
    app.setQuitOnLastWindowClosed(False)

    try:
        from keyboardmanager.core.backend import PynputBackend
        from keyboardmanager.core.hotkeys import create_hotkey_backend
        from keyboardmanager.core.service import MacroService
        from keyboardmanager.gui.mainwindow import MainWindow

        db = Database(args.db or default_db_path())
        service = MacroService(db, PynputBackend(), create_hotkey_backend(args.hotkey_backend))
        window = MainWindow(db, service)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, "Keyboard Manager", f"Failed to start: {exc}")
        return 1

    window.show()
    code = app.exec()
    service.shutdown()
    db.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
