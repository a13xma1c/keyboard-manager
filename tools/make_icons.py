"""Draw the application icon and write it in every format the bundles need.

Uses Qt (already a dependency) instead of image tools, so the icons can be built on
any operating system: ``app.icns`` (macOS), ``app.ico`` (Windows) and PNGs (Linux).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

PNG_SIZES = (16, 32, 64, 128, 256, 512, 1024)
BODY = QColor("#2f3640")
KEY = QColor("#f5f6fa")
ACCENT = QColor("#2f80ed")


def draw(size: int) -> QImage:
    """A keyboard with one highlighted key carrying the 'K' of Keyboard Manager."""
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 1024  # the drawing below is in a 1024 x 1024 grid

    body = QRectF(64 * s, 192 * s, 896 * s, 640 * s)
    path = QPainterPath()
    path.addRoundedRect(body, 96 * s, 96 * s)
    p.fillPath(path, QBrush(BODY))

    key, gap = 128 * s, 32 * s
    left, top = body.left() + 64 * s, body.top() + 80 * s
    for row in range(3):
        for column in range(5):
            if row == 2 and column not in (0, 4):  # space bar row
                continue
            rect = QRectF(left + column * (key + gap), top + row * (key + gap), key, key)
            if row == 2 and column == 4:
                rect.setRight(left + 4 * (key + gap) + key)
            highlighted = row == 1 and column == 2
            cell = QPainterPath()
            cell.addRoundedRect(rect, 24 * s, 24 * s)
            p.fillPath(cell, QBrush(ACCENT if highlighted else KEY))
            if highlighted and size >= 64:
                font = QFont()
                font.setPixelSize(int(96 * s))
                font.setBold(True)
                p.setFont(font)
                p.setPen(KEY)
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "K")
    if size >= 64:  # the space bar
        bar = QRectF(left + (key + gap), top + 2 * (key + gap), 3 * key + 2 * gap, key)
        space = QPainterPath()
        space.addRoundedRect(bar, 24 * s, 24 * s)
        p.fillPath(space, QBrush(KEY))
    p.end()
    return image


def write_icns(images: dict[int, QImage], out_dir: Path) -> Path:
    """macOS icon. Built with Apple's ``iconutil`` when available (it produces the
    multi-resolution format Finder expects); Qt's writer is the fallback elsewhere."""
    path = out_dir / "app.icns"
    iconutil = shutil.which("iconutil")
    if iconutil and sys.platform == "darwin":
        iconset = out_dir / "app.iconset"
        shutil.rmtree(iconset, ignore_errors=True)
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            images[size].save(str(iconset / f"icon_{size}x{size}.png"), "PNG")
            images[size * 2].save(str(iconset / f"icon_{size}x{size}@2x.png"), "PNG")
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(path)], check=True)
        shutil.rmtree(iconset, ignore_errors=True)
    else:
        assert images[1024].save(str(path), "ICNS"), path
    return path


def write_icons(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])  # noqa: F841 - needed for QImage/QFont
    written = []
    images = {size: draw(size) for size in PNG_SIZES}
    for size, image in images.items():
        path = out_dir / f"app_{size}.png"
        assert image.save(str(path), "PNG"), path
        written.append(path)
    ico = out_dir / "app.ico"
    assert images[256].save(str(ico), "ICO"), ico
    written.append(ico)
    written.append(write_icns(images, out_dir))
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent / "build" / "icons"
    for file in write_icons(target):
        print(f"{file}  ({file.stat().st_size} bytes)")
