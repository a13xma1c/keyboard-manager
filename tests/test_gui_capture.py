"""GUI side of vision: screen grabbing, image files and the Capture (snip) helper."""

import os

import pytest
from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QDialog, QFileDialog

from keyboardmanager.gui import capture
from keyboardmanager.gui.capture import (
    ImagePathEdit,
    SnipDialog,
    grab_screen,
    load_image,
    qimage_to_image,
    suggest_name,
)


def make_qimage(width=12, height=8):
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    for y in range(height):
        for x in range(width):
            img.setPixel(x, y, 0xFF000000 | (x * 7 % 256 << 16) | (y * 5 % 256 << 8) | 7)
    return img


def test_qimage_conversion_and_file_round_trip(qapp, tmp_path):
    qimage = make_qimage(5, 3)  # an odd width exercises row padding
    image = qimage_to_image(qimage)
    assert (image.width, image.height) == (5, 3)
    assert image.pixel(4, 2) == (28, 10, 7)
    path = tmp_path / "t.png"
    qimage.save(str(path), "PNG")
    assert load_image(str(path)) == image
    with pytest.raises(ValueError, match="cannot read"):
        load_image(str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="empty"):
        qimage_to_image(QImage())


def test_grab_screen_returns_a_screen(qapp):
    screen = grab_screen()  # the offscreen platform still provides one
    assert screen is not None and screen.width > 0 and screen.scale > 0


def test_suggest_name_counts_up(tmp_path):
    first = suggest_name(str(tmp_path))
    assert first.endswith("capture-1.png")
    (tmp_path / "capture-1.png").write_bytes(b"x")
    (tmp_path / "capture-7.png").write_bytes(b"x")
    (tmp_path / "unrelated.png").write_bytes(b"x")
    assert suggest_name(str(tmp_path)).endswith("capture-8.png")


def test_snip_dialog_crops_what_was_selected(qapp):
    dialog = SnipDialog(make_qimage(100, 60))
    assert dialog._factor == 1.0  # small screenshots are shown 1:1, never enlarged
    ok = dialog.buttons.button(dialog.buttons.StandardButton.Ok)
    assert not ok.isEnabled()
    dialog.area.selection = QRect(10, 20, 30, 15)
    dialog.area.selected.emit()
    assert ok.isEnabled()
    snippet = dialog.selected_image()
    assert (snippet.width(), snippet.height()) == (30, 15)
    assert qimage_to_image(snippet).pixel(0, 0) == (70, 100, 7)  # pixel (10, 20) of the original


def test_snip_dialog_scales_large_screenshots(qapp):
    dialog = SnipDialog(make_qimage(3200, 200))
    assert dialog._factor > 1.0
    dialog.area.selection = QRect(0, 0, 10, 10)
    snippet = dialog.selected_image()
    assert snippet.width() == round(10 * dialog._factor)


def test_snip_dialog_tiny_selection_is_not_accepted(qapp):
    dialog = SnipDialog(make_qimage())
    dialog.area.selection = QRect(1, 1, 1, 1)
    dialog.area.selected.emit()
    assert not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()


def test_image_path_edit_browse(qapp, monkeypatch):
    edit = ImagePathEdit()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("/tmp/x.png", "")))
    edit.browse_btn.click()
    assert edit.text() == "/tmp/x.png"
    edit.setText("typed.png")
    assert edit.text() == "typed.png"


def test_image_path_edit_capture_flow(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
    monkeypatch.setattr(capture, "grab_qimage", lambda: (make_qimage(50, 40), 1.0))
    target = str(tmp_path / "button.png")

    def exec_(dialog):
        dialog.area.selection = QRect(5, 5, 10, 8)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(SnipDialog, "exec", exec_)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (target, "")))
    edit = ImagePathEdit()
    edit.capture_btn.click()
    assert edit.text() == target and os.path.isfile(target)
    saved = load_image(target)
    assert (saved.width, saved.height) == (10, 8)
    assert saved.pixel(0, 0) == (35, 25, 7)  # pixel (5, 5) of the capture
    assert edit.capture_btn.isEnabled() and edit.capture_btn.text() == "Capture..."


def test_image_path_edit_capture_cancelled(qapp, monkeypatch):
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
    monkeypatch.setattr(capture, "grab_qimage", lambda: None)
    edit = ImagePathEdit()
    shown = []
    monkeypatch.setattr(capture.QMessageBox, "warning", lambda *a, **k: shown.append(a))
    edit.capture_btn.click()
    assert shown and edit.text() == ""
    monkeypatch.setattr(capture, "grab_qimage", lambda: (make_qimage(), 1.0))
    monkeypatch.setattr(SnipDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    edit.capture_btn.click()
    assert edit.text() == ""
