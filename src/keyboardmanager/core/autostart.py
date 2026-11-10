"""Start Keyboard Manager at login, in the background (no window, tray icon only).

* macOS    - a LaunchAgent: ``~/Library/LaunchAgents/com.keyboardmanager.app.plist``
* Windows  - the per-user ``Run`` registry key (HKCU\\...\\CurrentVersion\\Run)
* Linux    - an XDG autostart entry: ``~/.config/autostart/keyboardmanager.desktop``

All of them start the same program that is running now (the bundled application,
or ``python -m keyboardmanager`` from a source / pip installation) with
``--background``, plus ``--db`` when a non-default database is in use.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import sys
from pathlib import Path
from typing import Any

APP_ID = "com.keyboardmanager.app"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "KeyboardManager"


def launch_command(extra: list[str] | tuple[str, ...] = ("--background",)) -> list[str]:
    """How to start this installation again."""
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return [sys.executable, *extra]
    return [sys.executable, "-m", "keyboardmanager", *extra]


class Autostart:
    def __init__(self, platform: str | None = None, home: Path | None = None,
                 env: dict[str, str] | None = None, registry: Any = None) -> None:
        """Everything is injectable so every platform can be tested anywhere."""
        self.platform = platform or sys.platform
        self.home = Path(home) if home is not None else Path.home()
        self.env = env if env is not None else dict(os.environ)
        self._registry = registry

    # ------------------------------------------------------------ where
    @property
    def location(self) -> str:
        if self.platform == "darwin":
            return str(self._plist())
        if self.platform == "win32":
            return f"HKEY_CURRENT_USER\\{RUN_KEY}\\{RUN_VALUE}"
        return str(self._desktop_file())

    def _plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{APP_ID}.plist"

    def _desktop_file(self) -> Path:
        config = Path(self.env.get("XDG_CONFIG_HOME") or self.home / ".config")
        return config / "autostart" / "keyboardmanager.desktop"

    def _winreg(self) -> Any:
        if self._registry is None:
            import winreg  # type: ignore[import-not-found]

            self._registry = winreg
        return self._registry

    # ------------------------------------------------------------ state
    def is_enabled(self) -> bool:
        try:
            if self.platform == "darwin":
                return self._plist().exists()
            if self.platform == "win32":
                reg = self._winreg()
                with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    reg.QueryValueEx(key, RUN_VALUE)
                return True
            return self._desktop_file().exists()
        except OSError:
            return False

    def enable(self, command: list[str]) -> None:
        """Start ``command`` at login (replacing an older entry); raises OSError."""
        if self.platform == "darwin":
            path = self._plist()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as fh:
                plistlib.dump({"Label": APP_ID, "ProgramArguments": list(command), "RunAtLoad": True,
                               "ProcessType": "Interactive"}, fh)
        elif self.platform == "win32":
            reg = self._winreg()
            with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
                reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, _windows_command_line(command))
        else:
            path = self._desktop_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "[Desktop Entry]\nType=Application\nName=Keyboard Manager\n"
                f"Exec={' '.join(shlex.quote(part) for part in command)}\n"
                "X-GNOME-Autostart-enabled=true\nTerminal=false\n"
                "Comment=Keyboard shortcuts and macros (started in the background)\n",
                encoding="utf-8",
            )

    def disable(self) -> None:
        try:
            if self.platform == "darwin":
                self._plist().unlink()
            elif self.platform == "win32":
                reg = self._winreg()
                with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                    reg.DeleteValue(key, RUN_VALUE)
            else:
                self._desktop_file().unlink()
        except (FileNotFoundError, OSError):
            pass  # already off

    def command(self) -> list[str] | None:
        """The command currently registered (None when off or unreadable)."""
        try:
            if self.platform == "darwin":
                with self._plist().open("rb") as fh:
                    return list(plistlib.load(fh)["ProgramArguments"])
            if self.platform == "win32":
                reg = self._winreg()
                with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    return _split_windows(reg.QueryValueEx(key, RUN_VALUE)[0])
            for line in self._desktop_file().read_text(encoding="utf-8").splitlines():
                if line.startswith("Exec="):
                    return shlex.split(line[5:])
        except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
            return None
        return None


def _windows_command_line(command: list[str]) -> str:
    import subprocess

    return subprocess.list2cmdline(command)


def _split_windows(text: str) -> list[str]:
    out, current, quoted = [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif ch == " " and not quoted:
            if current:
                out.append(current)
            current = ""
        else:
            current += ch
    if current:
        out.append(current)
    return out
