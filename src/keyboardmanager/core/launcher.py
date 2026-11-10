"""Open an application, a file or a URL the way the operating system would."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from typing import Callable

_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://|^mailto:")


def launch_command(target: str, platform: str | None = None,
                   exists: Callable[[str], bool] = os.path.exists) -> list[str] | None:
    """The command that opens ``target`` (``None`` on Windows: ``os.startfile``)."""
    platform = platform or sys.platform
    target = target.strip()
    if not target:
        raise ValueError("nothing to open")
    is_document = bool(_URL.match(target)) or exists(os.path.expanduser(target))
    if platform == "win32":
        return None
    if platform == "darwin":
        # a URL or path opens in its default application; anything else is an app name
        return ["open", os.path.expanduser(target)] if is_document else ["open", "-a", target]
    if is_document:
        return ["xdg-open", os.path.expanduser(target)]
    return shlex.split(target)  # an application / command, e.g. "gedit notes.txt"


def launch(target: str) -> None:
    """Open ``target`` without waiting for it; raises ``OSError`` when impossible."""
    command = launch_command(target)
    if command is None:
        os.startfile(target.strip())  # type: ignore[attr-defined]  # Windows only
        return
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
