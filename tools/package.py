"""Build a distributable application for the machine this runs on.

    uv run python tools/package.py            # .app / .exe / binary + an archive
    uv run python tools/package.py --no-archive

Output goes to dist/: ``KeyboardManager.app`` + ``.dmg`` (macOS), ``KeyboardManager``
with ``KeyboardManager.exe`` + ``.zip`` (Windows) or a folder + ``.tar.gz`` (Linux).
Signing: set CODESIGN_IDENTITY (macOS) before running, see README.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "packaging"), str(ROOT / "tools")]

import appinfo  # noqa: E402
from make_icons import write_icons  # noqa: E402

DIST = ROOT / "dist"
SPEC = ROOT / "packaging" / "keyboardmanager.spec"


def build_icons() -> None:
    write_icons(appinfo.ICON_DIR)


def run_pyinstaller() -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"), str(SPEC)],
        check=True, cwd=ROOT,
    )


def bundle_path() -> Path:
    """What PyInstaller produced for this platform."""
    if sys.platform == "darwin":
        return DIST / f"{appinfo.BINARY_NAME}.app"
    return DIST / appinfo.BINARY_NAME


def archive(source: Path, version: str) -> Path:
    """Package the build into one file to upload or hand to a user."""
    platform = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    machine = os.uname().machine if hasattr(os, "uname") else os.environ.get("PROCESSOR_ARCHITECTURE", "x86_64")
    stem = DIST / f"{appinfo.BINARY_NAME}-{version}-{platform}-{machine.lower()}"

    if sys.platform == "darwin":
        target = stem.with_suffix(".dmg")
        target.unlink(missing_ok=True)
        subprocess.run(
            ["hdiutil", "create", "-volname", appinfo.APP_NAME, "-srcfolder", str(source),
             "-ov", "-format", "UDZO", str(target)],
            check=True,
        )
        return target
    if sys.platform == "win32":
        target = stem.with_suffix(".zip")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(source.rglob("*")):
                zf.write(path, Path(source.name) / path.relative_to(source))
        return target
    target = Path(f"{stem}.tar.gz")
    with tarfile.open(target, "w:gz") as tf:
        tf.add(source, arcname=source.name)
        tf.add(ROOT / "packaging" / "keyboardmanager.desktop", arcname=f"{source.name}/keyboardmanager.desktop")
    return target


def folder_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the desktop application")
    parser.add_argument("--no-archive", action="store_true", help="skip the .dmg / .zip / .tar.gz")
    args = parser.parse_args(argv)

    version = appinfo.version()
    print(f"Building {appinfo.APP_NAME} {version} for {sys.platform}")
    build_icons()
    run_pyinstaller()
    built = bundle_path()
    if not built.exists():
        print(f"error: {built} was not created", file=sys.stderr)
        return 1
    print(f"  {built}  ({folder_size_mb(built):.0f} MB)")
    if sys.platform == "darwin" and not os.environ.get("CODESIGN_IDENTITY"):
        print("  note: unsigned build - macOS will warn on first launch and may forget "
              "the Accessibility permission after every rebuild (see README)")
    if not args.no_archive:
        package = archive(built, version)
        print(f"  {package}  ({package.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
