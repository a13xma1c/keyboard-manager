"""Seeing the screen from the GUI side: grabbing, loading image files, and the
"Capture..." helper that lets the user snip a template image for the vision steps."""

from __future__ import annotations

import os
import re
import time

from PySide6.QtCore import QCoreApplication, QPoint, QRect, QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRubberBand,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core import vision

COUNTDOWN_S = 3  # before a capture, so the target window can be arranged


def qimage_to_image(qimage: QImage) -> vision.Image:
    """A ``QImage`` as the plain RGB image the matcher works on."""
    if qimage.isNull():
        raise ValueError("the image is empty")
    rgb = qimage.convertToFormat(QImage.Format.Format_RGB888)
    stride, width, height = rgb.bytesPerLine(), rgb.width(), rgb.height()
    buffer = bytes(rgb.constBits())
    if stride == width * 3:
        pixels = buffer[: width * height * 3]
    else:  # rows are padded: take each row's pixels only
        pixels = b"".join(buffer[y * stride: y * stride + width * 3] for y in range(height))
    return vision.Image(width, height, pixels)


def grab_qimage() -> tuple[QImage, float] | None:
    """The primary screen as a ``QImage`` plus its scale (capture pixels per point)."""
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return None
    pixmap = screen.grabWindow(0)
    if pixmap.isNull() or pixmap.width() < 1:
        return None
    scale = pixmap.width() / max(1, screen.geometry().width())
    return pixmap.toImage(), scale or 1.0


def grab_screen() -> vision.Screen | None:
    grabbed = grab_qimage()
    if grabbed is None:
        return None
    qimage, scale = grabbed
    image = qimage_to_image(qimage)
    return vision.Screen(image.width, image.height, image.pixels, scale)


def load_image(path: str) -> vision.Image:
    qimage = QImage(os.path.expanduser(path))
    if qimage.isNull():
        raise ValueError(f"cannot read the image file {path}")
    return qimage_to_image(qimage)


def images_dir() -> str:
    """Where captured templates are saved by default."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation) or os.path.expanduser("~")
    path = os.path.join(base, "images")
    os.makedirs(path, exist_ok=True)
    return path


def suggest_name(directory: str) -> str:
    """A free file name like ``capture-3.png`` in ``directory``."""
    taken = set(os.listdir(directory)) if os.path.isdir(directory) else set()
    numbers = [int(m.group(1)) for name in taken for m in [re.match(r"capture-(\d+)\.png$", name)] if m]
    return os.path.join(directory, f"capture-{max(numbers, default=0) + 1}.png")


class SnipArea(QLabel):
    """Shows a screenshot; the user drags a rectangle over it."""

    selected = Signal()

    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())
        self._band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self._origin = QPoint()
        self.selection = QRect()
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._origin = event.position().toPoint()
        self._band.setGeometry(QRect(self._origin, QSize()))
        self._band.show()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._band.isVisible():
            self._band.setGeometry(QRect(self._origin, event.position().toPoint()).normalized())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.selection = QRect(self._origin, event.position().toPoint()).normalized() & self.rect()
        self.selected.emit()


class SnipDialog(QDialog):
    """Select a part of a screenshot; ``selected_image()`` is the cropped QImage."""

    def __init__(self, qimage: QImage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Capture image")
        self._qimage = qimage
        pixmap = QPixmap.fromImage(qimage)
        available = QGuiApplication.primaryScreen().availableGeometry().size() if (
            QGuiApplication.primaryScreen()) else QSize(1200, 800)
        most = QSize(int(available.width() * 0.8), int(available.height() * 0.8))
        if pixmap.width() > most.width() or pixmap.height() > most.height():
            shown = pixmap.scaled(most, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        else:
            shown = pixmap  # never enlarged: selecting stays pixel-accurate
        self._factor = pixmap.width() / max(1, shown.width())  # shown -> capture pixels
        hint = QLabel("Drag a rectangle around what the macro should look for "
                      "(keep it small and distinctive, e.g. a button).")
        hint.setWordWrap(True)
        self.area = SnipArea(shown)
        self.area.selected.connect(self._update_ok)
        scroll = QScrollArea()
        scroll.setWidget(self.area)
        scroll.setWidgetResizable(False)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.buttons)
        self.resize(shown.width() + 48, shown.height() + 120)

    def _update_ok(self) -> None:
        good = self.area.selection.width() >= 2 and self.area.selection.height() >= 2
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(good)

    def selected_image(self) -> QImage:
        shown = self.area.selection
        rect = QRect(round(shown.x() * self._factor), round(shown.y() * self._factor),
                     round(shown.width() * self._factor), round(shown.height() * self._factor))
        return self._qimage.copy(rect & self._qimage.rect())


class ImagePathEdit(QWidget):
    """An image-file field: path, Browse..., and Capture... (snip from the screen)."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("path of a PNG to look for on the screen")
        self.edit.textChanged.connect(self.changed)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse)
        self.capture_btn = QPushButton("Capture...")
        self.capture_btn.setToolTip(f"After a {COUNTDOWN_S} second countdown the screen is captured "
                                    "and you select the part to look for")
        self.capture_btn.clicked.connect(self._capture)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.browse_btn)
        layout.addWidget(self.capture_btn)

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt style, like QLineEdit
        self.edit.setText(text)

    def _browse(self) -> None:
        start = self.edit.text().strip() or images_dir()
        path, _ = QFileDialog.getOpenFileName(self, "Image to look for", start,
                                              "Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self.edit.setText(path)

    def _capture(self) -> None:
        self._countdown = COUNTDOWN_S
        self.capture_btn.setEnabled(False)
        self._tick()

    def _tick(self) -> None:
        if self._countdown > 0:
            self.capture_btn.setText(f"Capturing in {self._countdown}...")
            self._countdown -= 1
            QTimer.singleShot(1000, self._tick)
            return
        self.capture_btn.setText("Capture...")
        self.capture_btn.setEnabled(True)
        # hide the application so it does not cover what should be captured
        hidden = [w for w in QGuiApplication.topLevelWindows() if w.isVisible()]
        for w in hidden:
            w.hide()
        try:
            QCoreApplication.processEvents()
            time.sleep(0.25)  # let the compositor really remove the windows
            grabbed = grab_qimage()
        finally:
            for w in hidden:
                w.show()
        if grabbed is None:
            QMessageBox.warning(self, "Capture", "Could not capture the screen on this system.")
            return
        dialog = SnipDialog(grabbed[0], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        snippet = dialog.selected_image()
        path, _ = QFileDialog.getSaveFileName(self, "Save the image as", suggest_name(images_dir()),
                                              "PNG image (*.png)")
        if not path:
            return
        if not snippet.save(path, "PNG"):
            QMessageBox.warning(self, "Capture", f"Could not save {path}.")
            return
        self.edit.setText(path)
