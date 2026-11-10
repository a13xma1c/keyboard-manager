"""Packaging: distribution metadata, the PyInstaller configuration, icons and the
build script. The spec file is executed with stand-in PyInstaller functions, so what
the bundles would contain is checked without building them (that takes minutes; the
real build runs in the release workflow and in tools/smoke_bundle.py)."""

from __future__ import annotations

import configparser
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "tools"))

import appinfo  # noqa: E402

PLATFORMS = ("darwin", "win32", "linux")


def pyproject() -> dict:
    tomllib = pytest.importorskip("tomllib")
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


# ------------------------------------------------------------------ metadata
def test_version_has_a_single_source():
    from keyboardmanager import __version__

    data = pyproject()
    assert "version" not in data["project"], "version must come from the package"
    assert data["tool"]["hatch"]["version"]["path"].endswith("keyboardmanager/__init__.py")
    assert appinfo.version() == __version__
    assert __version__.count(".") >= 1 and __version__[0].isdigit()


def test_installed_metadata_matches():
    from importlib.metadata import entry_points, version

    from keyboardmanager import __version__

    assert version("keyboardmanager") == __version__
    script = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert script["keyboardmanager"] == "keyboardmanager.app:main"


def test_version_option():
    from keyboardmanager import __version__

    result = subprocess.run([sys.executable, "-m", "keyboardmanager", "--version"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and __version__ in result.stdout


def test_project_metadata_is_complete():
    project = pyproject()["project"]
    assert project["license"] and (ROOT / "LICENSE").exists()
    assert project["readme"] == "README.md" and project["description"]
    assert any(c.startswith("Operating System :: MacOS") for c in project["classifiers"])
    assert project["requires-python"].startswith(">=3.10")


def test_sdist_contains_what_is_needed_to_build():
    include = pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert {"src", "tests", "packaging", "tools"} <= set(include)


# -------------------------------------------------- PyInstaller configuration
@pytest.mark.parametrize("platform", PLATFORMS)
def test_hidden_imports_cover_the_dynamic_backends(platform):
    modules = appinfo.hidden_imports(platform)
    backend = {"darwin": "darwin", "win32": "win32"}.get(platform, "xorg")
    # pynput picks its backend with importlib at run time, so PyInstaller cannot see it
    assert f"pynput.keyboard._{backend}" in modules and f"pynput.mouse._{backend}" in modules
    if platform == "darwin":
        assert {"AppKit", "Quartz", "HIServices"} <= set(modules)
        assert "keyboard" not in modules  # never imported on macOS
    if platform == "win32":
        assert "keyboard" in modules  # the default hotkey backend there


def test_excluded_modules_are_not_used_by_the_application():
    used = set()
    for file in (ROOT / "src").rglob("*.py"):
        for line in file.read_text().splitlines():
            line = line.strip()
            for prefix in ("import ", "from "):
                if line.startswith(prefix):
                    used.add(line[len(prefix):].split()[0])
    for excluded in appinfo.EXCLUDED_MODULES:
        assert excluded not in used, f"{excluded} is excluded from the bundle but used"
        assert not any(u.startswith(excluded + ".") for u in used), excluded


def run_spec(platform: str, monkeypatch, icon: Path | None = None) -> dict:
    """Execute the spec file with fake PyInstaller functions and collect the result."""
    result: dict = {}

    class Built:
        """Stands in for a PyInstaller object; any attribute the spec reads is fine."""

        def __init__(self, kind):
            self.kind = kind

        def __getattr__(self, item):
            return f"<{self.kind}.{item}>"

    def record(name):
        def call(*args, **kwargs):
            result[name] = {"args": args, **kwargs}
            return Built(name)

        return call

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(appinfo, "icon_file", lambda *a: icon)
    spec = ROOT / "packaging" / "keyboardmanager.spec"
    globals_ = {name: record(name) for name in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE")}
    globals_["SPECPATH"] = str(spec.parent)
    exec(compile(spec.read_text(), str(spec), "exec"), globals_)  # noqa: S102
    return result


@pytest.mark.parametrize("platform", PLATFORMS)
def test_spec_builds_a_windowless_app_with_the_right_pieces(platform, monkeypatch, tmp_path):
    icon = tmp_path / "app.icon"
    result = run_spec(platform, monkeypatch, icon)
    assert result["Analysis"]["args"][0] == [str(appinfo.ENTRY_POINT)]
    assert result["Analysis"]["hiddenimports"] == appinfo.hidden_imports(platform)
    assert result["Analysis"]["excludes"] == appinfo.EXCLUDED_MODULES
    assert result["EXE"]["console"] is False  # no terminal window for a GUI application
    assert result["EXE"]["name"] == appinfo.BINARY_NAME == "KeyboardManager"
    assert result["EXE"]["icon"] == str(icon)
    assert ("BUNDLE" in result) is (platform == "darwin")


def test_spec_works_without_icons(monkeypatch):
    result = run_spec("linux", monkeypatch, icon=None)
    assert result["EXE"]["icon"] is None


def test_spec_signs_the_mac_build_when_an_identity_is_given(monkeypatch):
    monkeypatch.setenv("CODESIGN_IDENTITY", "Developer ID Application: Someone (ABC123)")
    result = run_spec("darwin", monkeypatch)
    assert result["EXE"]["codesign_identity"] == "Developer ID Application: Someone (ABC123)"
    assert result["EXE"]["entitlements_file"].endswith("entitlements.plist")
    monkeypatch.delenv("CODESIGN_IDENTITY")
    assert run_spec("darwin", monkeypatch)["EXE"]["codesign_identity"] is None


def test_macos_bundle_description(monkeypatch):
    from keyboardmanager import __version__

    bundle = run_spec("darwin", monkeypatch)["BUNDLE"]
    assert bundle["bundle_identifier"] == appinfo.BUNDLE_ID == "io.github.keyboardmanager.app"
    assert bundle["version"] == __version__
    plist = bundle["info_plist"]
    # macOS remembers the Accessibility / Input Monitoring permission per bundle id,
    # so both places must carry the same stable value
    assert plist["CFBundleIdentifier"] == appinfo.BUNDLE_ID == bundle["bundle_identifier"]
    assert plist["CFBundleShortVersionString"] == __version__
    assert plist["LSMinimumSystemVersion"] == "14.0"  # the PySide6 wheels need macOS 14
    assert plist["LSUIElement"] is False  # it has a window, so it belongs in the Dock
    # macOS shows these sentences in the permission dialogs
    assert "Accessibility" in plist["NSSystemAdministrationUsageDescription"]
    assert plist["NSAppleEventsUsageDescription"].startswith("Keyboard Manager")


def test_entitlements_allow_what_a_hardened_runtime_would_block():
    import plistlib

    data = plistlib.loads((ROOT / "packaging" / "entitlements.plist").read_bytes())
    assert data["com.apple.security.cs.disable-library-validation"] is True
    assert "com.apple.security.app-sandbox" not in data  # sandboxing would break hotkeys


# --------------------------------------------------------------------- icons
def test_icons_are_generated_for_every_platform(qapp, tmp_path):
    from PySide6.QtGui import QImage

    from make_icons import PNG_SIZES, write_icons

    written = write_icons(tmp_path)
    assert {p.name for p in written} >= {"app.ico", "app.icns", *(f"app_{s}.png" for s in PNG_SIZES)}
    for size in PNG_SIZES:
        image = QImage(str(tmp_path / f"app_{size}.png"))
        assert not image.isNull() and image.size().width() == size
        assert image.pixelColor(size // 2, size // 2).alpha() > 0  # not an empty picture
    for name in ("app.ico", "app.icns"):
        assert (tmp_path / name).stat().st_size > 0
        assert not QImage(str(tmp_path / name)).isNull(), name
    assert (tmp_path / "app.icns").read_bytes()[:4] == b"icns"


@pytest.mark.parametrize("platform,name", [
    ("darwin", "app.icns"), ("win32", "app.ico"), ("linux", "app_512.png")])
def test_icon_file_per_platform(platform, name, monkeypatch, tmp_path):
    monkeypatch.setattr(appinfo, "ICON_DIR", tmp_path)
    assert appinfo.icon_file(platform) is None  # not generated yet
    (tmp_path / name).write_bytes(b"x")
    assert appinfo.icon_file(platform) == tmp_path / name


def test_linux_desktop_entry():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "packaging" / "keyboardmanager.desktop")
    entry = parser["Desktop Entry"]
    assert entry["Exec"] == appinfo.BINARY_NAME and entry["Type"] == "Application"
    assert entry["Terminal"] == "false" and entry["Categories"].endswith(";")


# -------------------------------------------------------------- build script
def test_build_script_names_and_archives(monkeypatch, tmp_path):
    import package

    monkeypatch.setattr(package, "DIST", tmp_path)
    built = tmp_path / appinfo.BINARY_NAME
    (built / "_internal").mkdir(parents=True)
    (built / appinfo.BINARY_NAME).write_bytes(b"binary")
    (built / "_internal" / "lib.so").write_bytes(b"x" * 2048)

    monkeypatch.setattr(sys, "platform", "linux")
    assert package.bundle_path() == built
    archive = package.archive(built, "9.9.9")
    assert archive.name == f"{appinfo.BINARY_NAME}-9.9.9-linux-{package.os.uname().machine.lower()}.tar.gz"
    import tarfile

    with tarfile.open(archive) as tf:
        names = tf.getnames()
    assert f"{appinfo.BINARY_NAME}/{appinfo.BINARY_NAME}" in names
    assert f"{appinfo.BINARY_NAME}/keyboardmanager.desktop" in names  # for the menu entry
    assert round(package.folder_size_mb(built)) == 0


def test_build_script_windows_archive(monkeypatch, tmp_path):
    import package
    import zipfile

    monkeypatch.setattr(package, "DIST", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    built = tmp_path / appinfo.BINARY_NAME
    built.mkdir()
    (built / f"{appinfo.BINARY_NAME}.exe").write_bytes(b"exe")
    archive = package.archive(built, "1.0.0")
    assert archive.suffix == ".zip"
    assert f"{appinfo.BINARY_NAME}/{appinfo.BINARY_NAME}.exe" in zipfile.ZipFile(archive).namelist()


def test_build_script_macos_archive(monkeypatch, tmp_path):
    import package

    monkeypatch.setattr(package, "DIST", tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    app = tmp_path / f"{appinfo.BINARY_NAME}.app"
    app.mkdir()
    assert package.bundle_path() == app
    commands = []
    monkeypatch.setattr(package.subprocess, "run",
                        lambda cmd, **kw: commands.append(cmd) or types.SimpleNamespace(returncode=0))
    archive = package.archive(app, "2.0.0")
    assert archive.suffix == ".dmg" and commands[0][0] == "hdiutil"
    assert "-srcfolder" in commands[0] and str(app) in commands[0]


def test_build_script_reports_a_failed_build(monkeypatch, tmp_path, capsys):
    import package

    monkeypatch.setattr(package, "DIST", tmp_path)
    monkeypatch.setattr(package, "build_icons", lambda: None)
    monkeypatch.setattr(package, "run_pyinstaller", lambda: None)  # produces nothing
    assert package.main(["--no-archive"]) == 1
    assert "was not created" in capsys.readouterr().err


def test_smoke_script_finds_the_bundle(monkeypatch, tmp_path):
    import smoke_bundle

    monkeypatch.setattr(smoke_bundle, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="no build found"):
        smoke_bundle.executable()
    built = tmp_path / "dist" / appinfo.BINARY_NAME
    built.mkdir(parents=True)
    (built / appinfo.BINARY_NAME).write_bytes(b"x")
    assert smoke_bundle.executable() == built / appinfo.BINARY_NAME
