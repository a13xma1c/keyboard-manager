"""Check that a built bundle actually starts (missing hidden imports only show here).

    uv run python tools/smoke_bundle.py [--seconds 8]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

import appinfo  # noqa: E402


def executable() -> Path:
    """The program inside the built bundle, whatever the platform calls it."""
    dist = ROOT / "dist"
    candidates = [
        dist / f"{appinfo.BINARY_NAME}.app" / "Contents" / "MacOS" / appinfo.BINARY_NAME,
        dist / appinfo.BINARY_NAME / f"{appinfo.BINARY_NAME}.exe",
        dist / appinfo.BINARY_NAME / appinfo.BINARY_NAME,
    ]
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit(f"no build found in {dist}; run tools/package.py first")


def check(seconds: float = 8.0) -> None:
    app = executable()
    print(f"testing {app}")

    help_run = subprocess.run([str(app), "--help"], capture_output=True, text=True, timeout=120)
    assert help_run.returncode == 0, help_run.stderr
    assert "keyboardmanager" in help_run.stdout, help_run.stdout
    print("  --help works")

    with tempfile.TemporaryDirectory() as tmp:
        process = subprocess.Popen(
            [str(app), "--db", str(Path(tmp) / "smoke.db")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(seconds)  # long enough to import Qt, open the window and register hotkeys
        alive = process.poll() is None
        process.terminate()
        try:
            output = process.communicate(timeout=30)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate()[0]
        assert alive, f"the application exited after launch:\n{output}"
        assert "Traceback" not in output, output
        assert (Path(tmp) / "smoke.db").exists(), "no database was created"
    print(f"  started and ran for {seconds:g}s, database created")
    print("bundle OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()
    os.environ.setdefault("KM_SMOKE", "1")
    check(args.seconds)
