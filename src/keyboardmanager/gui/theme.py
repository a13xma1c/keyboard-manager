"""The "Liquid Glass" look: translucent layered panels over a softly coloured
backdrop, capsule controls, floating toolbar and tab bar, accent-tinted selection.

Qt cannot blur what lies behind a widget, so the glass is layered the other way
round: the main window paints a smooth, already "blurred" backdrop (colour fields
derived from the accent colour, plus soft shadows under the floating panels), and
every panel is a translucent gradient with a bright top edge (the specular
highlight) on top of it. Everything else is a style sheet generated from one
palette, in a light and a dark variant, for any accent colour.

Appearance (setting ``appearance``):

* ``glass``        Liquid Glass, light or dark following the operating system
* ``glass-light`` / ``glass-dark``
* ``classic``      the platform's own controls (the previous look)
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPalette, QPen, \
    QRadialGradient, QLinearGradient
from PySide6.QtWidgets import QApplication, QMenu, QProxyStyle, QWidget

APPEARANCE_SETTING = "appearance"
ACCENT_SETTING = "accent"
APPEARANCES = {
    "glass": "Liquid Glass (follow the system)",
    "glass-light": "Liquid Glass - light",
    "glass-dark": "Liquid Glass - dark",
    "classic": "Classic (native controls)",
}
ACCENTS = {
    "system": "System accent", "blue": "#0a84ff", "purple": "#bf5af2", "pink": "#ff375f",
    "orange": "#ff9f0a", "green": "#30d158", "teal": "#32ade6", "graphite": "#8e8e93",
}
TONES = ("secondary", "danger", "warning", "success", "info")
PANEL_RADIUS = 22  # floating panels; controls inside use smaller, concentric radii
TOOLBAR_INSET = (12, 10, 12, 2)  # left, top, right, bottom: where the toolbar's glass pill sits


Rgba = tuple[int, int, int, int]


def rgba(c: Rgba) -> str:
    return f"rgba({c[0]}, {c[1]}, {c[2]}, {c[3]})"


def hex_rgb(text: str) -> tuple[int, int, int]:
    text = text.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


# ---------------------------------------------------------------- contrast
def composite(top: Rgba, below: tuple[int, int, int]) -> tuple[int, int, int]:
    """``top`` (with alpha) painted over an opaque colour."""
    a = top[3] / 255
    return tuple(round(t * a + b * (1 - a)) for t, b in zip(top[:3], below, strict=True))  # type: ignore[return-value]


def luminance(c: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG contrast ratio (1 to 21)."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ----------------------------------------------------------------- palette
@dataclass(frozen=True)
class GlassPalette:
    dark: bool
    accent: tuple[int, int, int]
    text: Rgba
    secondary: Rgba
    disabled: Rgba
    glass_top: Rgba          # panels (sidebar, tab page, toolbar)
    glass_bottom: Rgba
    sub_glass: Rgba          # group boxes inside panels
    popup: Rgba              # menus, combo lists, tooltips (nearly opaque)
    highlight: Rgba          # the bright top edge of glass
    edge: Rgba               # the outer edge of glass
    field: Rgba              # text fields and lists
    field_edge: Rgba
    control_top: Rgba        # buttons
    control_bottom: Rgba
    hover: Rgba
    pressed: Rgba
    separator: Rgba
    scroll: Rgba
    shadow: Rgba
    backdrop: tuple[tuple[int, int, int], tuple[int, int, int]]   # base gradient
    blobs: tuple[Rgba, ...]                                        # colour fields
    tones: dict

    @property
    def accent_hex(self) -> str:
        return "#{:02x}{:02x}{:02x}".format(*self.accent)

    def tone(self, name: str) -> str:
        return self.tones.get(name, rgba(self.text))

    def accent_soft(self, alpha: int) -> Rgba:
        return (*self.accent, alpha)

    @property
    def on_accent(self) -> tuple[int, int, int]:
        """Text on the solid accent: white, unless the accent is very light."""
        return (255, 255, 255) if contrast((255, 255, 255), self.accent) >= 2.6 else (20, 20, 24)

    @property
    def accent_text(self) -> tuple[int, int, int]:
        """The accent as a text colour readable on glass (darkened / lightened)."""
        base = composite(self.glass_bottom, self.backdrop[0])
        color = self.accent
        target = (255, 255, 255) if self.dark else (0, 0, 0)
        for step in range(0, 11):
            color = mix(self.accent, target, step / 10)
            if contrast(color, base) >= 4.5:
                break
        return color


def palette_for(dark: bool, accent: str = "#0a84ff") -> GlassPalette:
    acc = hex_rgb(accent)
    if dark:
        return GlassPalette(
            dark=True, accent=acc,
            text=(245, 245, 247, 255), secondary=(235, 235, 245, 195), disabled=(235, 235, 245, 90),
            glass_top=(58, 60, 72, 200), glass_bottom=(32, 34, 44, 195),
            sub_glass=(255, 255, 255, 14), popup=(38, 40, 50, 245),
            highlight=(255, 255, 255, 52), edge=(0, 0, 0, 110),
            field=(255, 255, 255, 12), field_edge=(255, 255, 255, 34),
            control_top=(255, 255, 255, 40), control_bottom=(255, 255, 255, 20),
            hover=(255, 255, 255, 26), pressed=(0, 0, 0, 60), separator=(255, 255, 255, 30),
            scroll=(255, 255, 255, 70), shadow=(0, 0, 0, 120),
            backdrop=((11, 14, 24), (22, 18, 36)),
            blobs=((*acc, 80), (191, 90, 242, 55), (50, 173, 230, 45), (255, 159, 10, 25)),
            tones={"secondary": rgba((235, 235, 245, 195)), "danger": "#ffa098", "warning": "#ffc14d",
                   "success": "#63e07e", "info": "#86c5ff"},
        )
    return GlassPalette(
        dark=False, accent=acc,
        text=(22, 22, 26, 255), secondary=(60, 60, 67, 215), disabled=(60, 60, 67, 95),
        glass_top=(255, 255, 255, 205), glass_bottom=(255, 255, 255, 165),
        sub_glass=(255, 255, 255, 110), popup=(252, 252, 254, 248),
        highlight=(255, 255, 255, 235), edge=(30, 40, 70, 38),
        field=(255, 255, 255, 165), field_edge=(30, 40, 70, 40),
        control_top=(255, 255, 255, 235), control_bottom=(255, 255, 255, 165),
        hover=(0, 0, 0, 14), pressed=(0, 0, 0, 32), separator=(60, 60, 67, 45),
        scroll=(60, 60, 67, 90), shadow=(30, 40, 80, 45),
        backdrop=((226, 233, 245), (243, 236, 245)),
        blobs=((*acc, 85), (255, 149, 0, 55), (52, 199, 89, 45), (175, 82, 222, 50)),
        tones={"secondary": rgba((60, 60, 67, 215)), "danger": "#b00017", "warning": "#953f00",
               "success": "#156a2b", "info": "#0050a8"},
    )


# --------------------------------------------------------- indicator images
_IMAGE_DIR: str | None = None


def _image_dir() -> str:
    global _IMAGE_DIR
    if _IMAGE_DIR is None or not os.path.isdir(_IMAGE_DIR):
        _IMAGE_DIR = tempfile.mkdtemp(prefix="keyboardmanager-theme-")
    return _IMAGE_DIR


def _paint_image(name: str, width: int, height: int, draw) -> str:
    """Render an indicator at 1x and 2x (Qt picks ``@2x`` on high-DPI screens)."""
    base = os.path.join(_image_dir(), name)
    for scale, suffix in ((1, ""), (2, "@2x")):
        path = f"{base}{suffix}.png"
        if os.path.exists(path):
            continue
        image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(scale, scale)
        draw(painter)
        painter.end()
        image.save(path, "PNG")
    return (base + ".png").replace("\\", "/")


def _qcolor(c: Rgba | tuple[int, int, int]) -> QColor:
    return QColor(*c)


def indicator_images(p: GlassPalette) -> dict[str, str]:
    """Switches, radio buttons, check marks and chevrons in the palette's colours."""
    key = f"{'d' if p.dark else 'l'}{p.accent_hex[1:]}"
    on_accent = p.on_accent
    track_off = (120, 120, 128, 90) if p.dark else (120, 120, 128, 60)

    def switch(on: bool, enabled: bool):
        def draw(painter: QPainter) -> None:
            painter.setOpacity(1.0 if enabled else 0.45)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor((*p.accent, 255) if on else track_off))
            painter.drawRoundedRect(QRectF(1, 1, 36, 22), 11, 11)
            x = 26 if on else 12
            painter.setBrush(QColor(0, 0, 0, 40))
            painter.drawEllipse(QPointF(x, 12.8), 9.4, 9.4)  # the knob's shadow
            painter.setBrush(QColor(255, 255, 255))
            painter.drawEllipse(QPointF(x, 12), 9, 9)
        return draw

    def radio(on: bool):
        def draw(painter: QPainter) -> None:
            if on:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_qcolor(p.accent))
                painter.drawEllipse(QPointF(9, 9), 8, 8)
                painter.setBrush(_qcolor(on_accent))
                painter.drawEllipse(QPointF(9, 9), 3.2, 3.2)
            else:
                painter.setPen(QPen(_qcolor(p.field_edge if p.dark else (60, 60, 67, 110)), 1.2))
                painter.setBrush(_qcolor(p.field))
                painter.drawEllipse(QPointF(9, 9), 7.4, 7.4)
        return draw

    def check(color):
        def draw(painter: QPainter) -> None:
            pen = QPen(_qcolor(color), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath(QPointF(3.5, 8.5))
            path.lineTo(6.8, 11.8)
            path.lineTo(12.5, 4.5)
            painter.drawPath(path)
        return draw

    def chevron(up: bool, color):
        def draw(painter: QPainter) -> None:
            pen = QPen(_qcolor(color), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            path = QPainterPath(QPointF(2.5, 6.5 if up else 3.5))
            path.lineTo(6, 3 if up else 7)
            path.lineTo(9.5, 6.5 if up else 3.5)
            painter.drawPath(path)
        return draw

    secondary = p.secondary
    return {
        "switch_on": _paint_image(f"switch-on-{key}", 38, 24, switch(True, True)),
        "switch_off": _paint_image(f"switch-off-{key}", 38, 24, switch(False, True)),
        "switch_on_disabled": _paint_image(f"switch-on-dis-{key}", 38, 24, switch(True, False)),
        "switch_off_disabled": _paint_image(f"switch-off-dis-{key}", 38, 24, switch(False, False)),
        "radio_on": _paint_image(f"radio-on-{key}", 18, 18, radio(True)),
        "radio_off": _paint_image(f"radio-off-{key}", 18, 18, radio(False)),
        "check": _paint_image(f"check-{key}", 16, 16, check(p.accent)),
        "check_on_accent": _paint_image(f"check-white-{key}", 16, 16, check(on_accent)),
        "chevron_down": _paint_image(f"chev-down-{key}", 12, 10, chevron(False, secondary)),
        "chevron_up": _paint_image(f"chev-up-{key}", 12, 10, chevron(True, secondary)),
    }


# -------------------------------------------------------------- style sheet
def glass_stylesheet(p: GlassPalette) -> str:
    """The whole Liquid Glass style sheet for ``p``."""
    img = indicator_images(p)
    text, secondary, disabled = rgba(p.text), rgba(p.secondary), rgba(p.disabled)
    glass = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {rgba(p.glass_top)}, stop:1 {rgba(p.glass_bottom)})"
    control = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {rgba(p.control_top)}, "
               f"stop:1 {rgba(p.control_bottom)})")
    accent = p.accent_hex
    lighter = "#{:02x}{:02x}{:02x}".format(*mix(p.accent, (255, 255, 255), 0.18))
    darker = "#{:02x}{:02x}{:02x}".format(*mix(p.accent, (0, 0, 0), 0.18))
    accent_fill = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {lighter}, stop:1 {accent})"
    on_accent = "#{:02x}{:02x}{:02x}".format(*p.on_accent)
    accent_text = "#{:02x}{:02x}{:02x}".format(*p.accent_text)
    soft = rgba(p.accent_soft(70 if p.dark else 45))
    softer = rgba(p.accent_soft(45 if p.dark else 28))
    lens = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {rgba(p.accent_soft(95 if p.dark else 60))}, "
            f"stop:1 {rgba(p.accent_soft(55 if p.dark else 30))})")
    base0, base1 = p.backdrop
    dialog_bg = (f"qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgb{base0}, "
                 f"stop:0.55 rgb{composite(p.glass_top, mix(base0, base1, 0.5))}, stop:1 rgb{base1})")
    edge, highlight, field, field_edge = rgba(p.edge), rgba(p.highlight), rgba(p.field), rgba(p.field_edge)
    popup, separator, hover, pressed = rgba(p.popup), rgba(p.separator), rgba(p.hover), rgba(p.pressed)
    tones = "\n".join(f'*[tone="{name}"] {{ color: {p.tone(name)}; }}' for name in TONES)
    return f"""
/* ---------- Liquid Glass ({'dark' if p.dark else 'light'}) ---------- */
QWidget {{ color: {text}; }}
QWidget:disabled {{ color: {disabled}; }}
QMainWindow {{ background: transparent; }}
QDialog, QMessageBox, QInputDialog, QFileDialog {{ background: {dialog_bg}; }}
QLabel {{ background: transparent; }}
{tones}
*[tone="secondary"]:disabled {{ color: {disabled}; }}

/* floating panels */
QWidget#glassSidebar {{
    background: {glass}; border: 1px solid {edge}; border-top-color: {highlight};
    border-radius: {PANEL_RADIUS}px;
}}
QTabWidget#mainTabs::pane {{
    background: {glass}; border: 1px solid {edge}; border-top-color: {highlight};
    border-radius: {PANEL_RADIUS}px; top: 8px; padding: 6px;
}}
QTabWidget::pane {{ border: none; background: transparent; }}
QWidget#teamBanner {{ background: {soft}; border: 1px solid {softer}; border-radius: 14px; }}
QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget, QSplitter {{ background: transparent; }}
QSplitter::handle {{ background: transparent; }}

/* floating toolbar */
QToolBar#glassToolbar {{
    background: transparent; border: none; padding: {TOOLBAR_INSET[1] + 4}px {TOOLBAR_INSET[0] + 8}px
    {TOOLBAR_INSET[3] + 4}px {TOOLBAR_INSET[2] + 8}px; spacing: 3px;
}}
QToolBar#glassToolbar QToolButton {{
    background: transparent; border: 1px solid transparent; border-radius: 14px;
    padding: 5px 10px; margin: 1px;
}}
QToolBar#glassToolbar QToolButton:hover {{ background: {hover}; }}
QToolBar#glassToolbar QToolButton:pressed {{ background: {pressed}; }}
QToolBar#glassToolbar QToolButton:checked {{
    background: {lens}; border: 1px solid {highlight}; color: {accent_text};
}}
QToolBar#glassToolbar QToolButton:disabled {{ color: {disabled}; }}
QToolBar::separator {{ background: {separator}; width: 1px; margin: 7px 6px; }}
QToolBar QComboBox, QToolBar QDoubleSpinBox {{ margin: 1px 2px; }}

/* tab bar: a floating capsule of segments; the selected one is a tinted lens */
QTabWidget::tab-bar {{ alignment: center; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: {glass}; color: {secondary}; border: 1px solid {edge}; border-top-color: {highlight};
    padding: 6px 14px; margin: 0 -1px 0 0; min-width: 20px;
}}
QTabBar::tab:first {{ border-top-left-radius: 16px; border-bottom-left-radius: 16px; padding-left: 16px; }}
QTabBar::tab:last {{ border-top-right-radius: 16px; border-bottom-right-radius: 16px; padding-right: 16px; }}
QTabBar::tab:only-one {{ border-radius: 16px; }}
QTabBar::tab:hover {{ color: {text}; }}
QTabBar::tab:selected {{ background: {lens}; color: {accent_text}; font-weight: 600; }}
QTabBar::tab:disabled {{ color: {disabled}; }}
QTabBar QToolButton {{ background: {glass}; border: 1px solid {edge}; border-radius: 11px; margin: 2px; }}

/* group boxes: glass inside glass */
QGroupBox {{
    background: {rgba(p.sub_glass)}; border: 1px solid {separator}; border-radius: 16px;
    margin-top: 18px; padding: 8px 6px 6px 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left; left: 14px; top: 1px;
    color: {secondary}; font-weight: 600; padding: 0 2px;
}}

/* capsule buttons */
QPushButton {{
    background: {control}; color: {text}; border: 1px solid {edge}; border-top-color: {highlight};
    border-radius: 13px; padding: 4px 14px; min-height: 18px;
}}
QPushButton:hover {{ border-color: {rgba(p.accent_soft(120))}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:checked {{ background: {lens}; color: {accent_text}; }}
QPushButton:disabled {{ background: {field}; color: {disabled}; border-color: {separator}; }}
QPushButton:default, QPushButton[default="true"] {{
    background: {accent_fill}; color: {on_accent}; border: 1px solid {darker}; font-weight: 600;
}}
QPushButton:default:hover {{ background: {accent}; }}
QPushButton:default:pressed {{ background: {darker}; }}
QPushButton:default:disabled {{ background: {soft}; color: {disabled}; border-color: transparent; }}
QPushButton:flat {{ background: transparent; border: none; }}
QToolButton {{
    background: {control}; color: {text}; border: 1px solid {edge}; border-top-color: {highlight};
    border-radius: 12px; padding: 4px 10px;
}}
QToolButton:hover {{ border-color: {rgba(p.accent_soft(120))}; }}
QToolButton:pressed {{ background: {pressed}; }}
QToolButton:checked {{ background: {lens}; color: {accent_text}; border-color: {highlight}; }}
QToolButton:disabled {{ background: {field}; color: {disabled}; border-color: {separator}; }}
QToolButton[popupMode="1"] {{ padding-right: 22px; }}
QToolButton::menu-button {{ border: none; border-left: 1px solid {separator}; width: 18px; }}
QToolButton::menu-indicator, QPushButton::menu-indicator {{
    image: url({img['chevron_down']}); subcontrol-position: right center; subcontrol-origin: padding;
    right: 6px; width: 10px; height: 8px;
}}
QToolButton::menu-arrow {{ image: url({img['chevron_down']}); width: 10px; height: 8px; }}

/* fields */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QTimeEdit, QDateEdit,
QComboBox {{
    background: {field}; color: {text}; border: 1px solid {field_edge}; border-radius: 9px;
    padding: 3px 8px; selection-background-color: {accent}; selection-color: {on_accent};
}}
QPlainTextEdit, QTextEdit {{ padding: 4px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateTimeEdit:focus, QTimeEdit:focus, QDateEdit:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QDateTimeEdit:disabled {{ color: {disabled}; background: {rgba(p.sub_glass)}; }}
QLineEdit:read-only {{ background: {rgba(p.sub_glass)}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ border: none; width: 24px; subcontrol-origin: padding; subcontrol-position: right center; }}
QComboBox::down-arrow {{ image: url({img['chevron_down']}); width: 12px; height: 10px; }}
QComboBox QAbstractItemView {{
    background: {popup}; color: {text}; border: 1px solid {edge}; border-radius: 12px; padding: 4px;
    selection-background-color: {accent}; selection-color: {on_accent}; outline: 0;
}}
QSpinBox, QDoubleSpinBox, QDateTimeEdit, QTimeEdit {{ padding-right: 18px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QDateTimeEdit::up-button, QTimeEdit::up-button {{
    subcontrol-origin: border; subcontrol-position: top right; width: 16px; border: none;
    background: transparent; margin-top: 2px; margin-right: 3px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button, QDateTimeEdit::down-button, QTimeEdit::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right; width: 16px; border: none;
    background: transparent; margin-bottom: 2px; margin-right: 3px;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateTimeEdit::up-arrow, QTimeEdit::up-arrow {{
    image: url({img['chevron_up']}); width: 10px; height: 8px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateTimeEdit::down-arrow, QTimeEdit::down-arrow {{
    image: url({img['chevron_down']}); width: 10px; height: 8px;
}}

/* switches and radio buttons */
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 38px; height: 24px; }}
QCheckBox::indicator:unchecked {{ image: url({img['switch_off']}); }}
QCheckBox::indicator:checked {{ image: url({img['switch_on']}); }}
QCheckBox::indicator:unchecked:disabled {{ image: url({img['switch_off_disabled']}); }}
QCheckBox::indicator:checked:disabled {{ image: url({img['switch_on_disabled']}); }}
QRadioButton::indicator {{ width: 18px; height: 18px; }}
QRadioButton::indicator:unchecked {{ image: url({img['radio_off']}); }}
QRadioButton::indicator:checked {{ image: url({img['radio_on']}); }}

/* lists, trees and tables */
QAbstractItemView {{
    background: {field}; alternate-background-color: {rgba(p.accent_soft(10 if p.dark else 8))}; color: {text};
    border: 1px solid {field_edge}; border-radius: 14px; padding: 4px; outline: 0;
    selection-background-color: {soft}; selection-color: {text};
}}
QAbstractItemView::item {{ padding: 3px 6px; }}
/* rounded only when hovered / selected: a radius on every item would hide item colours set by the app */
QAbstractItemView::item:hover {{ background: {hover}; border-radius: 8px; }}
QAbstractItemView::item:selected {{ background: {soft}; color: {text}; border-radius: 8px; }}
QAbstractItemView::item:selected:active {{ background: {rgba(p.accent_soft(95 if p.dark else 62))}; }}
QTreeView {{ show-decoration-selected: 0; }}
QTreeView::branch {{ background: transparent; }}
QTreeView::branch:selected {{ background: transparent; }}
QTableView {{ gridline-color: {separator}; selection-background-color: transparent; }}
QTableView::item:hover {{ background: {hover}; border-radius: 0; }}
/* a table paints the selection of a row and of its cell: half strength each */
QTableView::item:selected {{ background: {rgba(p.accent_soft(36 if p.dark else 24))}; color: {text}; border-radius: 0; }}
QTableView::item:selected:active {{ background: {rgba(p.accent_soft(52 if p.dark else 34))}; }}
QHeaderView {{ background: transparent; border: none; border-radius: 0; padding: 0; }}
QHeaderView::section {{ background: transparent; }}
QHeaderView::section {{
    color: {secondary}; border: none; border-bottom: 1px solid {separator}; padding: 5px 8px; font-weight: 600;
}}
QTableCornerButton::section {{ background: transparent; border: none; }}

/* overlay-like scroll bars */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 6px 2px 6px 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0 6px 2px 6px; }}
QScrollBar::handle:vertical {{ background: {rgba(p.scroll)}; border-radius: 4px; min-height: 32px; margin: 0 1px; }}
QScrollBar::handle:horizontal {{ background: {rgba(p.scroll)}; border-radius: 4px; min-width: 32px; margin: 1px 0; }}
QScrollBar::handle:hover {{ background: {secondary}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; background: none; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* menus and tool tips: the most opaque glass */
QMenu {{
    background: {popup}; color: {text}; border: 1px solid {edge}; border-radius: 14px; padding: 6px;
}}
QMenu::item {{ padding: 6px 28px 6px 14px; border-radius: 8px; background: transparent; }}
QMenu::item:selected {{ background: {accent}; color: {on_accent}; }}
QMenu::item:disabled {{ color: {disabled}; }}
QMenu::separator {{ height: 1px; background: {separator}; margin: 5px 10px; }}
QMenu::indicator {{ width: 16px; height: 16px; left: 6px; }}
QMenu::indicator:checked {{ image: url({img['check']}); }}
QMenu::indicator:checked:selected {{ image: url({img['check_on_accent']}); }}
QMenu::right-arrow {{ width: 8px; height: 8px; }}
QToolTip {{
    background: {popup}; color: {text}; border: 1px solid {edge}; border-radius: 10px; padding: 5px 8px;
}}

QStatusBar {{ background: transparent; color: {secondary}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {secondary}; padding: 0 6px; }}
QProgressBar {{ background: {field}; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
"""


def classic_stylesheet() -> str:
    """The classic look keeps the platform's controls; only the semantic colours."""
    return """
*[tone="secondary"] { color: gray; }
*[tone="danger"] { color: #c62828; }
*[tone="warning"] { color: #ef6c00; }
*[tone="success"] { color: #2e7d32; }
*[tone="info"] { color: #1565c0; }
QWidget#teamBanner { background: #e3f2fd; border-radius: 4px; }
QWidget#teamBanner QLabel { color: #0d2a4a; }
"""


# ---------------------------------------------------------------- backdrop
def paint_backdrop(painter: QPainter, rect, p: GlassPalette, panels: list | None = None) -> None:
    """The coloured "wallpaper" behind the glass, plus soft shadows under ``panels``
    (rectangles in the painter's coordinates)."""
    r = QRectF(rect)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = QLinearGradient(r.topLeft(), r.bottomRight())
    base.setColorAt(0, QColor(*p.backdrop[0]))
    base.setColorAt(1, QColor(*p.backdrop[1]))
    painter.fillRect(r, QBrush(base))
    spots = ((0.12, 0.18, 0.55), (0.88, 0.82, 0.6), (0.78, 0.12, 0.42), (0.22, 0.92, 0.45))
    for (fx, fy, size), color in zip(spots, p.blobs, strict=False):
        center = QPointF(r.left() + r.width() * fx, r.top() + r.height() * fy)
        radius = max(r.width(), r.height()) * size
        gradient = QRadialGradient(center, radius)
        gradient.setColorAt(0, QColor(*color))
        faded = QColor(*color)
        faded.setAlpha(0)
        gradient.setColorAt(1, faded)
        painter.fillRect(r, QBrush(gradient))
    for panel in panels or []:
        _soft_shadow(painter, QRectF(panel), p.shadow)
    painter.restore()


def paint_glass(painter: QPainter, rect: QRectF, p: GlassPalette, radius: float) -> None:
    """A glass panel drawn directly (for what a style sheet cannot place, like the
    toolbar's floating capsule): translucent fill, outer edge, bright top edge."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    fill.setColorAt(0, QColor(*p.glass_top))
    fill.setColorAt(1, QColor(*p.glass_bottom))
    painter.setBrush(QBrush(fill))
    painter.setPen(QPen(QColor(*p.edge), 1))
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    shine = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    shine.setColorAt(0, QColor(*p.highlight))
    faded = QColor(*p.highlight)
    faded.setAlpha(0)
    shine.setColorAt(0.45, faded)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QBrush(shine), 1))
    painter.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), radius - 1, radius - 1)
    painter.restore()


def _soft_shadow(painter: QPainter, rect: QRectF, color: Rgba, spread: int = 14) -> None:
    """A blurred-looking shadow: nested rounded rectangles with falling alpha."""
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(spread, 0, -2):
        alpha = round(color[3] * (1 - i / spread) ** 2 / 3)
        if alpha <= 0:
            continue
        painter.setBrush(QColor(color[0], color[1], color[2], alpha))
        grown = rect.adjusted(-i * 0.6, -i * 0.3, i * 0.6, i * 1.0)
        painter.drawRoundedRect(grown, PANEL_RADIUS + i * 0.6, PANEL_RADIUS + i * 0.6)


# ------------------------------------------------------------------ manager
def system_is_dark(palette: QPalette | None = None) -> bool:
    """The operating system's colour scheme; without that information, the
    platform's own palette (``palette``: as it was before any theme was applied)."""
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", lambda: None)()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    if palette is None:
        app = QGuiApplication.instance()
        palette = app.palette() if app is not None else QPalette()
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


class ThemeManager(QObject):
    """Applies the appearance to the whole application (one per application)."""

    changed = Signal()
    _instance: "ThemeManager | None" = None

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.app = app
        self._original_style = app.style().name()
        self.style_name = self._original_style  # the style set by this manager (app.style() hides it)
        self._style: QProxyStyle | None = None
        self._original_palette = QPalette(app.palette())
        self._system_accent = self._original_palette.color(QPalette.ColorRole.Highlight)
        self.appearance = "classic"
        self.accent = "system"
        self.palette: GlassPalette | None = None
        self._key: tuple | None = None
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self._system_changed())

    @classmethod
    def instance(cls) -> "ThemeManager":
        app = QApplication.instance()
        if cls._instance is None or cls._instance.app is not app:
            cls._instance = ThemeManager(app)  # type: ignore[arg-type]
        return cls._instance

    @property
    def glass(self) -> bool:
        return self.palette is not None

    def accent_hex(self) -> str:
        if self.accent in ACCENTS and self.accent != "system":
            return ACCENTS[self.accent]
        if self.accent.startswith("#") and len(self.accent) == 7:
            return self.accent
        color = self._system_accent
        if not color.isValid() or color.saturation() < 40:  # a grey "accent": use blue
            return ACCENTS["blue"]
        return color.name()

    def apply(self, appearance: str = "glass", accent: str = "system") -> None:
        if appearance not in APPEARANCES:
            appearance = "glass"
        self.appearance, self.accent = appearance, accent
        if appearance == "classic":
            key: tuple = ("classic",)
        else:
            dark = {"glass-dark": True, "glass-light": False}.get(appearance)
            if dark is None:
                dark = system_is_dark(self._original_palette)  # not our own (themed) palette
            key = ("glass", dark, self.accent_hex())
        if key == self._key:
            return
        self._key = key
        if appearance == "classic":
            self.palette = None
            self.app.setStyle(self._original_style)
            self._style = None
            self.style_name = self._original_style
            self.app.setPalette(self._original_palette)
            self.app.setStyleSheet(classic_stylesheet())
        else:
            self.palette = palette_for(key[1], key[2])
            if self._style is None:
                # Fusion: the platform styles ignore much of a style sheet
                self._style = _GlassStyle()  # keep the Python object: it holds the override
                self.app.setStyle(self._style)
                self.style_name = "fusion"
            self.app.setPalette(qt_palette(self.palette))
            self.app.setStyleSheet(glass_stylesheet(self.palette))
        self.changed.emit()

    def _system_changed(self) -> None:
        if self.appearance == "glass":
            self.apply(self.appearance, self.accent)



class _GlassStyle(QProxyStyle):
    """Fusion, plus see-through menu windows so their rounded corners show."""

    def __init__(self) -> None:
        super().__init__("Fusion")

    def polish(self, target):  # noqa: D401 - Qt API with overloads (widget, palette, application)
        result = super().polish(target)
        if isinstance(target, QMenu):
            target.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        return result


def qt_palette(p: GlassPalette) -> QPalette:
    """A QPalette matching the glass colours, for what the style sheet does not cover."""
    pal = QPalette()
    text = QColor(*p.text)
    base = composite(p.glass_bottom, p.backdrop[0])
    window = QColor(*mix(p.backdrop[0], p.backdrop[1], 0.5))
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        pal.setColor(group, QPalette.ColorRole.Window, window)
        pal.setColor(group, QPalette.ColorRole.WindowText, text)
        pal.setColor(group, QPalette.ColorRole.Base, QColor(*base))
        pal.setColor(group, QPalette.ColorRole.AlternateBase, QColor(*mix(base, p.text[:3], 0.04)))
        pal.setColor(group, QPalette.ColorRole.Text, text)
        pal.setColor(group, QPalette.ColorRole.Button, QColor(*composite(p.control_top, base)))
        pal.setColor(group, QPalette.ColorRole.ButtonText, text)
        pal.setColor(group, QPalette.ColorRole.Highlight, QColor(*p.accent))
        pal.setColor(group, QPalette.ColorRole.HighlightedText, QColor(*p.on_accent))
        pal.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(*composite(p.popup, base)))
        pal.setColor(group, QPalette.ColorRole.ToolTipText, text)
        pal.setColor(group, QPalette.ColorRole.PlaceholderText, QColor(*composite(p.secondary, base)))
        pal.setColor(group, QPalette.ColorRole.Link, QColor(*p.accent_text))
    disabled = QColor(*composite(p.disabled, base))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return pal


# ------------------------------------------------------------------ helpers
def set_tone(widget: QWidget, tone: str | None) -> None:
    """Colour a label by meaning ("secondary", "danger", ...); follows the theme."""
    if tone is not None and tone not in TONES:
        raise ValueError(f"unknown tone {tone!r}")
    widget.setProperty("tone", tone or "")
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def tone_color(tone: str) -> QColor:
    """A tone as a colour for painting (e.g. table cells), for the current theme."""
    manager = ThemeManager._instance
    if manager is not None and manager.palette is not None:
        return QColor(manager.palette.tone(tone)) if tone != "secondary" else QColor(*manager.palette.secondary)
    return QColor({"secondary": "#808080", "danger": "#c62828", "warning": "#ef6c00",
                   "success": "#2e7d32", "info": "#1565c0"}.get(tone, "#000000"))


def is_dark() -> bool:
    manager = ThemeManager._instance
    return bool(manager is not None and manager.palette is not None and manager.palette.dark)
