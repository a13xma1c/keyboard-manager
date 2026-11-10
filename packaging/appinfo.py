"""Everything the bundle builders need to know, as plain data (no PyInstaller import).

Kept separate from ``keyboardmanager.spec`` so it can be unit tested: the spec file
itself is only executed by PyInstaller.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Keyboard Manager"        # shown to users (macOS app, window, menus)
BINARY_NAME = "KeyboardManager"      # file name of the executable / .app
BUNDLE_ID = "io.github.keyboardmanager.app"
ENTRY_POINT = ROOT / "src" / "keyboardmanager" / "__main__.py"
ICON_DIR = ROOT / "build" / "icons"
MIN_MACOS = "14.0"  # the macOS version the bundled PySide6 requires


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from keyboardmanager import __version__

        return __version__
    finally:
        sys.path.pop(0)


def hidden_imports(platform: str | None = None) -> list[str]:
    """Modules imported at run time by name, which PyInstaller cannot see."""
    platform = platform or sys.platform
    backend = {"darwin": "darwin", "win32": "win32"}.get(platform, "xorg")
    modules = [
        f"pynput.keyboard._{backend}",
        f"pynput.mouse._{backend}",
        f"pynput._util.{backend}",
    ]
    if platform == "darwin":
        # pyobjc frameworks used through `import X` inside functions
        modules += ["AppKit", "Quartz", "HIServices", "objc", "CoreFoundation"]
    elif platform != "win32":
        modules += ["Xlib.keysymdef.xkb"]  # python-xlib loads keysym tables by name
    else:
        modules += ["keyboard"]
    return modules


# Qt modules this application never uses; leaving them out halves the bundle size
EXCLUDED_MODULES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth", "PySide6.QtNetworkAuth",
    "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "tkinter", "unittest", "pydoc", "pytest", "numpy",
]


def icon_file(platform: str | None = None) -> Path | None:
    """Icon for the bundle, or ``None`` when it has not been generated yet."""
    platform = platform or sys.platform
    name = {"darwin": "app.icns", "win32": "app.ico"}.get(platform, "app_512.png")
    path = ICON_DIR / name
    return path if path.exists() else None


def macos_info_plist(app_version: str | None = None) -> dict:
    """Info.plist of the .app bundle.

    The usage descriptions are what macOS shows when it asks for permission, and the
    bundle identifier is what the Accessibility / Input Monitoring settings remember,
    so it must stay stable across releases.
    """
    app_version = app_version or version()
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleShortVersionString": app_version,
        "CFBundleVersion": app_version,
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSHighResolutionCapable": True,
        # Keyboard Manager keeps running in the menu bar, but it is a normal app
        # with a window, so it stays in the Dock (LSUIElement would hide it).
        "LSUIElement": False,
        "NSAppleEventsUsageDescription":
            f"{APP_NAME} needs this to detect which application is in front, so macros "
            "can be limited to specific applications.",
        "NSSystemAdministrationUsageDescription":
            f"{APP_NAME} needs Accessibility and Input Monitoring access to send "
            "keystrokes, record input and use global hotkeys.",
    }
