"""Operating-system permissions needed for global input (macOS privacy settings).

Without them macOS does not raise errors: hotkeys never fire, recordings stay empty
and injected keys are dropped. So the application checks up front and tells the
user exactly what to enable. Only *preflight* checks are used (they never show
system prompts), and anything that cannot be determined counts as granted so
users are never nagged by mistake.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Permission:
    name: str
    needed_for: str
    settings: str


ACCESSIBILITY = Permission(
    "Accessibility",
    "running macros (sending keys and mouse input) and global hotkeys",
    "System Settings > Privacy & Security > Accessibility",
)
INPUT_MONITORING = Permission(
    "Input Monitoring",
    "global hotkeys and recording",
    "System Settings > Privacy & Security > Input Monitoring",
)


def missing_permissions(platform: str | None = None) -> list[Permission]:
    if (platform or sys.platform) != "darwin":
        return []
    missing: list[Permission] = []
    try:
        import HIServices  # type: ignore[import-not-found]  # pyobjc, as used by pynput

        if not HIServices.AXIsProcessTrusted():
            missing.append(ACCESSIBILITY)
    except Exception:  # noqa: BLE001
        pass
    try:
        import Quartz  # type: ignore[import-not-found]

        preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)  # macOS 10.15+
        if preflight is not None and not preflight():
            missing.append(INPUT_MONITORING)
    except Exception:  # noqa: BLE001
        pass
    return missing


def explain(missing: list[Permission]) -> str:
    lines = ["Keyboard Manager is missing macOS permissions:", ""]
    for p in missing:
        lines.append(f"\u2022 {p.name} - needed for {p.needed_for}.\n   {p.settings}")
    lines += [
        "",
        "Enable them for the application that starts Keyboard Manager (for example "
        "Terminal, iTerm or your Python app), then restart Keyboard Manager.",
    ]
    return "\n".join(lines)
