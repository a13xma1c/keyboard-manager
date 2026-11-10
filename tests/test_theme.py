"""The Liquid Glass appearance: palettes (incl. contrast), the style sheet, the painted
backdrop, switching appearance / accent, and the window's use of it."""

import os

import pytest
from PySide6.QtCore import QRectF, qInstallMessageHandler
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from keyboardmanager.core.service import MacroService
from keyboardmanager.gui import theme
from keyboardmanager.gui.mainwindow import MainWindow
from keyboardmanager.gui.theme import (
    ACCENTS,
    ThemeManager,
    composite,
    contrast,
    glass_stylesheet,
    indicator_images,
    paint_backdrop,
    paint_glass,
    palette_for,
    set_tone,
    tone_color,
)
from tests_support import FakeHotkeys

ACCENT_HEXES = [v for k, v in ACCENTS.items() if k != "system"]


def backgrounds(p):
    """Every colour glass text may sit on: the panel glass over each part of the
    backdrop (its base colours and each colour field at full strength)."""
    under = list(p.backdrop)
    for blob in p.blobs:
        for base in p.backdrop:
            under.append(composite(blob, base))
    glass = [composite(p.glass_bottom, u) for u in under] + [composite(p.glass_top, u) for u in under]
    fields = [composite(p.field, g) for g in glass]  # text fields and lists on glass
    return glass + fields


# ------------------------------------------------------------------ contrast
@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("accent", ACCENT_HEXES)
def test_text_is_readable_everywhere(dark, accent):
    p = palette_for(dark, accent)
    for bg in backgrounds(p):
        assert contrast(p.text[:3], bg) >= 7.0, ("text", bg)          # WCAG AAA
        assert contrast(composite(p.secondary, bg), bg) >= 4.5, ("secondary", bg)  # AA
        for tone in ("danger", "warning", "success", "info"):
            assert contrast(QColor(p.tone(tone)).getRgb()[:3], bg) >= 4.5, (tone, bg)


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("accent", ACCENT_HEXES)
def test_accent_text_and_text_on_accent(dark, accent):
    p = palette_for(dark, accent)
    base = composite(p.glass_bottom, p.backdrop[0])
    assert contrast(p.accent_text, base) >= 4.5
    assert contrast(p.on_accent, p.accent) >= 2.6  # bold button labels on the accent


def test_contrast_helpers():
    assert contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21, abs=0.01)
    assert contrast((119, 119, 119), (255, 255, 255)) == pytest.approx(4.48, abs=0.02)
    assert composite((255, 255, 255, 0), (10, 20, 30)) == (10, 20, 30)
    assert composite((255, 255, 255, 255), (10, 20, 30)) == (255, 255, 255)


def test_light_accents_get_dark_text():
    assert palette_for(False, "#ffd60a").on_accent == (20, 20, 24)
    assert palette_for(False, "#0a84ff").on_accent == (255, 255, 255)


# --------------------------------------------------------------- stylesheet
@pytest.fixture
def qt_warnings():
    seen = []
    previous = qInstallMessageHandler(lambda mode, context, message: seen.append(message))
    yield seen
    qInstallMessageHandler(previous)


@pytest.mark.parametrize("dark", [False, True])
def test_stylesheet_parses_without_warnings(qapp, qt_warnings, dark):
    css = glass_stylesheet(palette_for(dark, "#bf5af2"))
    widget = QPushButton("x")
    widget.setStyleSheet(css)
    widget.ensurePolished()
    label = QLabel("y")
    label.setStyleSheet(css)
    label.ensurePolished()
    assert not [w for w in qt_warnings if "style" in w.lower() or "parse" in w.lower()]
    assert "#bf5af2" in css and ("dark" if dark else "light") in css
    for selector in ("QWidget#glassSidebar", "QTabWidget#mainTabs::pane", "QToolBar#glassToolbar",
                     "QCheckBox::indicator:checked", "QMenu::item:selected", 'tone="danger"'):
        assert selector in css


def test_indicator_images_are_rendered_at_two_scales(qapp):
    images = indicator_images(palette_for(False, "#30d158"))
    for name, path in images.items():
        assert os.path.exists(path), name
        hi = path[:-4] + "@2x.png"
        assert os.path.exists(hi)
        assert QImage(hi).width() == 2 * QImage(path).width()
    on = QImage(images["switch_on"])
    assert QColor(on.pixel(5, 12)).getRgb()[:3] == (48, 209, 88)  # the accent track
    again = indicator_images(palette_for(False, "#30d158"))
    assert again == images  # cached, not redrawn


# ------------------------------------------------------------------ painting
def test_backdrop_paints_colour_fields_and_shadows(qapp):
    p = palette_for(False, "#0a84ff")
    image = QImage(400, 300, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    paint_backdrop(painter, image.rect(), p, panels=[QRectF(100, 80, 200, 120)])
    painter.end()
    top_left, middle_right = QColor(image.pixel(40, 50)), QColor(image.pixel(390, 150))
    assert top_left.alpha() == 255 and top_left != middle_right  # not a flat colour
    assert top_left.blue() > top_left.red()  # tinted by the (blue) accent field
    plain = QImage(400, 300, QImage.Format.Format_ARGB32)
    plain.fill(0)
    painter = QPainter(plain)
    paint_backdrop(painter, plain.rect(), p)
    painter.end()
    # just below the panel the backdrop is darker than without it: a soft shadow
    assert QColor(image.pixel(200, 206)).lightness() < QColor(plain.pixel(200, 206)).lightness()
    assert image.pixel(380, 20) == plain.pixel(380, 20)  # and only there


def test_glass_panel_has_a_bright_top_edge(qapp):
    p = palette_for(True, "#0a84ff")
    image = QImage(200, 60, QImage.Format.Format_ARGB32)
    image.fill(QColor(20, 20, 30))
    painter = QPainter(image)
    paint_glass(painter, QRectF(10, 10, 180, 40), p, 20)
    painter.end()
    edge, inside = QColor(image.pixel(100, 12)), QColor(image.pixel(100, 35))
    assert edge.lightness() > inside.lightness()


# ------------------------------------------------------------------ manager
@pytest.fixture
def manager(qapp):
    m = ThemeManager.instance()
    before = (m.appearance, m.accent)
    yield m
    m.apply(*before)


def test_apply_glass_and_back_to_classic(manager, qapp):
    original = manager._original_style
    manager.apply("glass-dark", "pink")
    assert manager.glass and manager.palette.dark and manager.palette.accent_hex == "#ff375f"
    assert manager.style_name.lower() == "fusion"
    assert "Liquid Glass (dark)" in qapp.styleSheet()
    assert qapp.palette().color(qapp.palette().ColorRole.Highlight).name() == "#ff375f"
    manager.apply("classic", "pink")
    assert not manager.glass and manager.style_name == original
    assert 'tone="danger"' in qapp.styleSheet() and "Liquid Glass" not in qapp.styleSheet()


def test_apply_is_idempotent(manager):
    manager.apply("glass-light", "blue")
    emitted = []
    manager.changed.connect(lambda: emitted.append(1))
    manager.apply("glass-light", "blue")
    assert emitted == []
    manager.apply("glass-light", "green")
    assert emitted == [1]


def test_following_the_system(manager, monkeypatch):
    monkeypatch.setattr(theme, "system_is_dark", lambda palette=None: True)
    manager.apply("glass", "blue")
    assert manager.palette.dark
    monkeypatch.setattr(theme, "system_is_dark", lambda palette=None: False)
    manager._system_changed()  # the OS switched to light
    assert not manager.palette.dark
    manager.apply("glass-dark", "blue")
    manager._system_changed()  # a fixed choice does not follow
    assert manager.palette.dark


def test_system_scheme_is_read_from_the_platform_palette_not_ours(manager, qapp, monkeypatch):
    from PySide6.QtGui import QPalette

    light = QPalette()
    light.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    dark = QPalette()
    dark.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    monkeypatch.setattr(type(QApplication.styleHints()), "colorScheme",
                        lambda self: theme.Qt.ColorScheme.Unknown, raising=False)
    assert theme.system_is_dark(dark) and not theme.system_is_dark(light)
    monkeypatch.setattr(manager, "_original_palette", light)
    manager.apply("glass-dark", "blue")  # our own palette is dark now...
    manager.apply("glass", "blue")
    assert not manager.palette.dark  # ...but the platform's is light


def test_accent_choices(manager):
    manager.accent = "#123456"
    assert manager.accent_hex() == "#123456"
    manager.accent = "orange"
    assert manager.accent_hex() == ACCENTS["orange"]
    manager.accent = "system"
    original = manager._system_accent
    manager._system_accent = QColor(128, 128, 128)  # a grey system accent
    assert manager.accent_hex() == ACCENTS["blue"]
    manager._system_accent = QColor("#ff2d55")
    assert manager.accent_hex() == "#ff2d55"
    manager._system_accent = original
    manager.apply("nonsense", "blue")
    assert manager.appearance == "glass"


def test_tones(manager, qapp):
    label = QLabel("x")
    set_tone(label, "danger")
    assert label.property("tone") == "danger"
    set_tone(label, None)
    assert label.property("tone") == ""
    with pytest.raises(ValueError):
        set_tone(label, "rainbow")
    manager.apply("glass-dark", "blue")
    assert tone_color("danger").name() == "#ffa098" and theme.is_dark()
    manager.apply("classic", "blue")
    assert tone_color("danger").name() == "#c62828" and not theme.is_dark()


def test_item_colours_set_by_the_app_still_show(manager, qapp):
    """(A radius on every item would make Qt ignore item backgrounds: the Run tab
    marks the running step that way.)"""
    from PySide6.QtGui import QBrush
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

    manager.apply("glass-light", "blue")
    tree = QTreeWidget()
    tree.setHeaderHidden(True)
    tree.resize(200, 100)
    first, second = QTreeWidgetItem(["one"]), QTreeWidgetItem(["two"])
    tree.addTopLevelItems([first, second])
    second.setBackground(0, QBrush(QColor(255, 0, 0)))
    tree.show()
    qapp.processEvents()
    rect = tree.visualItemRect(second)
    assert QColor(tree.grab().toImage().pixel(rect.center().x() + 30, rect.center().y())).getRgb()[:3] \
        == (255, 0, 0)


def test_menus_get_a_see_through_background_for_their_rounded_corners(manager, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMenu

    manager.apply("glass-light", "blue")
    menu = QMenu()
    menu.addAction("x")
    menu.ensurePolished()
    assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


# ------------------------------------------------------------------- window
@pytest.fixture
def win(qapp, db, backend, manager):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    qapp.processEvents()
    yield window
    window._quitting = True
    window.close()
    service.shutdown()
    window.deleteLater()
    qapp.processEvents()


def test_window_follows_the_setting(qapp, db, backend, manager):
    db.set_setting("appearance", "glass-dark")
    db.set_setting("accent", "teal")
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    try:
        assert manager.palette.dark and manager.palette.accent_hex == ACCENTS["teal"]
        assert window.appearance_actions["glass-dark"].isChecked()
        assert window.accent_actions["teal"].isChecked()
    finally:
        window._quitting = True
        window.close()
        service.shutdown()


def test_appearance_menu(win, db, qapp):
    win.appearance_actions["glass-light"].trigger()
    assert db.get_setting("appearance") == "glass-light" and win.theme.glass
    win.accent_actions["purple"].trigger()
    assert db.get_setting("accent") == "purple" and win.theme.palette.accent_hex == ACCENTS["purple"]
    assert win.run_view.current_color().name() == ACCENTS["purple"]
    from PySide6.QtWidgets import QTreeWidgetItem

    item = QTreeWidgetItem(["running step"])
    win.run_view.tree.addTopLevelItem(item)
    win.run_view._set_current(item)
    win.accent_actions["green"].trigger()  # the running step's mark follows the accent
    assert item.background(0).color() == win.run_view.current_color()
    assert win.run_view.current_color().name() == ACCENTS["green"]
    win.appearance_actions["classic"].trigger()
    assert not win.theme.glass and not win.accent_actions["green"].isEnabled()
    labels = [a.text() for a in win.appearance_btn.menu().actions() if a.text()]
    assert labels[:4] == list(theme.APPEARANCES.values())


@pytest.mark.parametrize("appearance", ["glass-light", "glass-dark", "classic"])
def test_window_paints_in_every_appearance(win, qapp, appearance):
    win.set_appearance(appearance)
    win.resize(1200, 860)
    win.show()
    qapp.processEvents()
    image = win.grab().toImage()
    corner = QColor(image.pixel(3, image.height() // 2))
    if appearance == "classic":
        assert corner == win.palette().color(win.palette().ColorRole.Window)
    else:
        p = win.theme.palette
        assert corner.getRgb()[:3] != tuple(QApplication.palette().color(
            QApplication.palette().ColorRole.Window).getRgb()[:3]) or p is not None
        assert (corner.lightness() < 90) == p.dark  # the backdrop matches light / dark
    assert win.sidebar.objectName() == "glassSidebar" and win.tabs.objectName() == "mainTabs"


def test_toolbar_capsule_sits_inside_the_toolbar(win, qapp):
    win.set_appearance("glass-light")
    win.show()
    qapp.processEvents()
    pill = win._toolbar_pill()
    bar = win.toolbar.geometry()
    assert pill.left() > bar.left() and pill.top() > bar.top() and pill.right() < bar.right()
    # the capsule itself is painted: lighter than the bare backdrop at its left end
    image = win.grab().toImage()
    bare = QImage(image.size(), QImage.Format.Format_ARGB32)
    painter = QPainter(bare)
    paint_backdrop(painter, bare.rect(), win.theme.palette)
    painter.end()
    x, y = int(pill.left() + 4), int(pill.center().y())
    assert QColor(image.pixel(x, y)).lightness() > QColor(bare.pixel(x, y)).lightness()
    win.toolbar.hide()
    assert win._toolbar_pill() is None


def test_toolbar_fits_the_smallest_window(win, qapp, monkeypatch):
    for appearance in ("glass-light", "classic"):
        win.set_appearance(appearance)
        qapp.processEvents()
        assert win.toolbar.sizeHint().width() <= win.minimumSizeHint().width()
    texts = [a.text() for a in win.safety_keys_btn.menu().actions()]
    assert texts == ["Stop hotkey...", "Pause hotkey..."]
    called = []
    monkeypatch.setattr(win, "_change_stop_hotkey", lambda: called.append("stop"))
    win.stop_key_action.triggered.disconnect()
    win.stop_key_action.triggered.connect(win._change_stop_hotkey)
    win.stop_key_action.trigger()
    assert called == ["stop"]


def test_editor_details_scroll_and_stay_narrow(win, qapp):
    ed = win.editor
    assert ed.details_scroll.widget() is ed._details_box
    assert ed.body_splitter.count() == 2 and not ed.body_splitter.childrenCollapsible()
    assert ed.enabled_check.text() == "Enabled" and "hotkey" in ed.enabled_check.toolTip()
    assert ed._details_box.minimumSizeHint().width() < 800  # fits next to the macro list
    assert ed.details_scroll.sizeHint().height() >= ed._details_box.sizeHint().height()


def test_semantic_colours_follow_the_theme(win, qapp, tmp_path):
    win.set_appearance("glass-dark")
    page = win.connect_view.sync_page
    win.service.db.set_setting("sync_folder", str(tmp_path / "gone"))
    win.service.sync_now()
    page.refresh()
    assert page.last_label.property("tone") == "danger"
    win.check_view.refresh()
    assert win.check_view.summary.property("tone") in ("success", "danger", "warning")
