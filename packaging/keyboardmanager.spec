# PyInstaller spec: run it with `python tools/package.py` (which also makes the icons).
# All decisions live in packaging/appinfo.py so they can be unit tested.
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH)))
import appinfo  # noqa: E402

icon = appinfo.icon_file()
analysis = Analysis(
    [str(appinfo.ENTRY_POINT)],
    pathex=[str(appinfo.ROOT / "src")],
    hiddenimports=appinfo.hidden_imports(),
    excludes=appinfo.EXCLUDED_MODULES,
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=appinfo.BINARY_NAME,
    console=False,                 # no terminal window on Windows / macOS
    icon=str(icon) if icon else None,
    target_arch=None,              # the architecture of the machine doing the build
    codesign_identity=os.environ.get("CODESIGN_IDENTITY") or None,
    entitlements_file=str(Path(SPECPATH) / "entitlements.plist"),
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    name=appinfo.BINARY_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        collect,
        name=f"{appinfo.BINARY_NAME}.app",
        icon=str(icon) if icon else None,
        bundle_identifier=appinfo.BUNDLE_ID,
        version=appinfo.version(),
        info_plist=appinfo.macos_info_plist(),
    )
