"""Reusable Qt widgets: key capture field and the action parameter dialog."""

from __future__ import annotations

import sys

from PySide6.QtCore import QDateTime, QEvent, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QCursor, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import keys
from keyboardmanager.core.actions import ActionSpec, Field
from keyboardmanager.core.models import Action
from keyboardmanager.core.schedule import (
    AT_FORMAT,
    DAY_NAMES,
    MAX_INTERVAL_MINUTES,
    Schedule,
    parse_at,
    parse_clock,
)

_K = Qt.Key
_IS_MAC = sys.platform == "darwin"


def _modifier_keys() -> dict:
    # Qt reports Command as "Control" and Control as "Meta" on macOS.
    return {
        _K.Key_Control: "cmd" if _IS_MAC else "ctrl",
        _K.Key_Meta: "ctrl" if _IS_MAC else "cmd",
        _K.Key_Alt: "alt",
        _K.Key_AltGr: "alt",
        _K.Key_Shift: "shift",
    }

_NAMED = {
    _K.Key_Return: "enter", _K.Key_Enter: "enter", _K.Key_Escape: "esc", _K.Key_Tab: "tab",
    _K.Key_Backtab: "tab", _K.Key_Space: "space", _K.Key_Backspace: "backspace",
    _K.Key_Delete: "delete", _K.Key_Insert: "insert", _K.Key_Home: "home", _K.Key_End: "end",
    _K.Key_PageUp: "page_up", _K.Key_PageDown: "page_down", _K.Key_Up: "up", _K.Key_Down: "down",
    _K.Key_Left: "left", _K.Key_Right: "right", _K.Key_CapsLock: "caps_lock",
    _K.Key_NumLock: "num_lock", _K.Key_ScrollLock: "scroll_lock", _K.Key_Pause: "pause",
    _K.Key_Print: "print_screen", _K.Key_Menu: "menu", _K.Key_Plus: "plus",
}


def qt_key_name(key: int) -> str | None:
    """Canonical name for a ``Qt.Key`` value, or ``None`` if unsupported."""
    try:
        qk = _K(key)
    except ValueError:
        qk = None
    modifiers = _modifier_keys()
    if qk in modifiers:
        return modifiers[qk]
    if qk in _NAMED:
        return _NAMED[qk]
    if _K.Key_F1 <= key <= _K.Key_F24:
        return f"f{key - _K.Key_F1 + 1}"
    if 0x20 < key <= 0x7E:
        return chr(key).lower()
    return None


def qt_modifier_names(event: QKeyEvent) -> list[str]:
    mods = event.modifiers()
    names = []
    ctrl = Qt.KeyboardModifier.MetaModifier if _IS_MAC else Qt.KeyboardModifier.ControlModifier
    cmd = Qt.KeyboardModifier.ControlModifier if _IS_MAC else Qt.KeyboardModifier.MetaModifier
    if mods & ctrl:
        names.append("ctrl")
    if mods & Qt.KeyboardModifier.AltModifier:
        names.append("alt")
    if mods & Qt.KeyboardModifier.ShiftModifier:
        names.append("shift")
    if mods & cmd:
        names.append("cmd")
    return names


class KeyCaptureEdit(QWidget):
    """Text field plus a *Capture* button that records the next key combination.

    ``mode``: ``hotkey`` (modifiers + one key), ``combo`` (same, any text accepted)
    or ``key`` (a single key, modifiers allowed on their own).
    """

    changed = Signal()

    def __init__(self, mode: str = "hotkey", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self._capturing = False
        self._pending: str | None = None
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("e.g. ctrl+alt+k" if mode != "key" else "e.g. shift")
        self.edit.textChanged.connect(lambda _t: self.changed.emit())
        self.button = QPushButton("Capture")
        self.button.setCheckable(True)
        self.button.toggled.connect(self._toggle)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.button.installEventFilter(self)
        self.edit.installEventFilter(self)

    # ------------------------------------------------------------------- value
    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, text: str) -> None:
        self.edit.setText(text)

    def normalized(self) -> str:
        """Canonical form; raises ``ValueError`` when the text is not a valid key spec."""
        text = self.text()
        if self.mode == "hotkey":
            return keys.normalize_hotkey(text)
        if self.mode == "key":
            return keys.normalize_key(text)
        return keys.normalize_combo(text)

    # ----------------------------------------------------------------- capture
    def _toggle(self, on: bool) -> None:
        self._capturing = on
        self._pending = None
        self.button.setText("Press keys..." if on else "Capture")
        if on:
            self.button.setFocus()

    def _finish(self, text: str) -> None:
        self.edit.setText(text)
        self.button.setChecked(False)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        if self._capturing and obj is self.button:
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self._on_key_press(event)
                return True
            if event.type() == QEvent.Type.KeyRelease and not event.isAutoRepeat():
                if self._pending and self.mode == "key" and qt_key_name(event.key()) == self._pending:
                    self._finish(self._pending)  # a lone modifier was tapped
                return True
        if self._capturing and event.type() == QEvent.Type.FocusOut and obj is self.button:
            self.button.setChecked(False)
        return super().eventFilter(obj, event)

    def _on_key_press(self, event: QKeyEvent) -> None:
        name = qt_key_name(event.key())
        if name is None:
            return
        if name in keys.MODIFIERS:
            self._pending = name
            # some platforms leave the modifier being pressed out of modifiers()
            held = set(qt_modifier_names(event)) | {name}
            self.button.setText("+".join(m for m in keys.MODIFIERS if m in held) + "+...")
            return
        self._pending = None
        if self.mode == "key":
            self._finish(name)
        else:
            self._finish("+".join([*qt_modifier_names(event), name]))


class ActionDialog(QDialog):
    """Edit the parameters of a single action; the form is generated from its spec."""

    def __init__(self, action: Action, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec: ActionSpec = catalog.spec_for(action.type)
        self.setWindowTitle(self.spec.label)
        self.setMinimumWidth(440)
        self._widgets: dict[str, QWidget] = {}
        self._rows: dict[str, tuple[QWidget, QWidget | None]] = {}
        self._action = action

        layout = QVBoxLayout(self)
        if self.spec.help:
            help_label = QLabel(self.spec.help)
            help_label.setWordWrap(True)
            layout.addWidget(help_label)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        for f in self.spec.fields:
            widget = self._make_widget(f, catalog.get_param(action, f.name))
            self._widgets[f.name] = widget
            label = QLabel(f.label)
            self.form.addRow(label, widget)
            self._rows[f.name] = (label, widget)
            if f.hint:
                widget.setToolTip(f.hint)
        if {"x", "y"} <= self._widgets.keys():
            pick = QPushButton("Pick position (3 s countdown)")
            pick.clicked.connect(self._pick_position)
            self.form.addRow("", pick)
            self._pick = pick

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_visibility()

    # --------------------------------------------------------------- widgets
    def _make_widget(self, f: Field, value) -> QWidget:
        if f.kind in ("combo", "key"):
            w = KeyCaptureEdit("combo" if f.kind == "combo" else "key")
            w.setText(str(value))
            w.changed.connect(self._update_visibility)
            return w
        if f.kind == "int":
            w = QSpinBox()
            w.setRange(int(f.minimum), int(f.maximum))
            w.setValue(int(value))
            w.valueChanged.connect(self._update_visibility)
            return w
        if f.kind == "float":
            w = QDoubleSpinBox()
            w.setRange(f.minimum, f.maximum)
            w.setValue(float(value))
            return w
        if f.kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            w.toggled.connect(self._update_visibility)
            return w
        if f.kind == "choice":
            w = QComboBox()
            w.addItems(f.choices)
            w.setCurrentText(str(value))
            w.currentTextChanged.connect(self._update_visibility)
            return w
        w = QLineEdit(str(value))
        w.textChanged.connect(self._update_visibility)
        return w

    @staticmethod
    def _read(f: Field, w: QWidget):
        if isinstance(w, KeyCaptureEdit):
            return w.text()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QDoubleSpinBox):
            return w.value()
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentText()
        return w.text()  # type: ignore[attr-defined]

    def values(self) -> dict:
        return {f.name: self._read(f, self._widgets[f.name]) for f in self.spec.fields}

    def _update_visibility(self, *_args) -> None:
        values = self.values()
        for f in self.spec.fields:
            visible = True
            if f.show_if:
                other, accepted = f.show_if
                visible = values.get(other) in accepted
            label, widget = self._rows[f.name]
            label.setVisible(visible)
            widget.setVisible(visible)

    def _pick_position(self) -> None:
        self._countdown = 3
        self._pick.setEnabled(False)
        self._tick()

    def _tick(self) -> None:
        if self._countdown > 0:
            self._pick.setText(f"Move the mouse to the target... {self._countdown}")
            self._countdown -= 1
            QTimer.singleShot(1000, self._tick)
            return
        pos = QCursor.pos()
        self._widgets["x"].setValue(pos.x())  # type: ignore[attr-defined]
        self._widgets["y"].setValue(pos.y())  # type: ignore[attr-defined]
        self._pick.setText("Pick position (3 s countdown)")
        self._pick.setEnabled(True)

    # --------------------------------------------------------------- result
    def _accept(self) -> None:
        candidate = Action(self._action.type, self.values())
        problems = catalog.validate_action(candidate)
        if problems:
            QMessageBox.warning(self, "Invalid action", "\n".join(p.split(": ", 1)[-1] for p in problems))
            return
        for f in self.spec.fields:
            w = self._widgets[f.name]
            if isinstance(w, KeyCaptureEdit):
                try:
                    candidate.params[f.name] = w.normalized()
                except ValueError as exc:
                    QMessageBox.warning(self, "Invalid key", str(exc))
                    return
        self._action.params = candidate.params
        self.accept()


class ScheduleEdit(QWidget):
    """Pick when a macro runs by itself: never, once, on weekdays at a time, or every N minutes."""

    changed = Signal()
    KINDS = (
        ("Not scheduled", "none"),
        ("Once", "once"),
        ("Days of the week", "weekly"),
        ("Repeatedly", "interval"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind_combo = QComboBox()
        for label, kind in self.KINDS:
            self.kind_combo.addItem(label, kind)

        self.at_edit = QDateTimeEdit()
        self.at_edit.setCalendarPopup(True)
        self.at_edit.setDisplayFormat("yyyy-MM-dd HH:mm")

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        digits = self.fontMetrics().horizontalAdvance("00:00")
        self.time_edit.setMinimumWidth(digits + 40)
        self.at_edit.setMinimumWidth(self.fontMetrics().horizontalAdvance("0000-00-00 00:00") + 50)
        self.day_buttons: list[QToolButton] = []
        weekly = QWidget()
        weekly_row = QHBoxLayout(weekly)
        weekly_row.setContentsMargins(0, 0, 0, 0)
        weekly_row.addWidget(QLabel("at"))
        weekly_row.addWidget(self.time_edit)
        weekly_row.addWidget(QLabel("on"))
        for name in DAY_NAMES:
            b = QToolButton()
            b.setText(name)
            b.setCheckable(True)
            b.setToolTip("Click to include / exclude this day")
            b.toggled.connect(self._emit)
            self.day_buttons.append(b)
            weekly_row.addWidget(b)
        weekly_row.addStretch(1)
        # make the selected days unmistakable in every style
        weekly.setStyleSheet(
            "QToolButton { padding: 2px 4px; }"
            "QToolButton:checked { background: palette(highlight); color: palette(highlighted-text);"
            " border: 1px solid palette(highlight); border-radius: 3px; }"
        )

        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(1, MAX_INTERVAL_MINUTES)
        self.minutes_spin.setSuffix(" min")
        self.minutes_spin.setToolTip("Counted from when the schedule becomes active (start, save)")
        interval = QWidget()
        interval_row = QHBoxLayout(interval)
        interval_row.setContentsMargins(0, 0, 0, 0)
        interval_row.addWidget(QLabel("every"))
        interval_row.addWidget(self.minutes_spin)
        interval_row.addStretch(1)

        once = QWidget()
        once_row = QHBoxLayout(once)
        once_row.setContentsMargins(0, 0, 0, 0)
        once_row.addWidget(QLabel("on"))
        once_row.addWidget(self.at_edit)
        once_row.addStretch(1)

        self.pages = QStackedWidget()
        for page in (QWidget(), once, weekly, interval):
            self.pages.addWidget(page)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.kind_combo)
        row.addWidget(self.pages, 1)

        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.at_edit.dateTimeChanged.connect(self._emit)
        self.time_edit.timeChanged.connect(self._emit)
        self.minutes_spin.valueChanged.connect(self._emit)
        self._loading = False
        self.set_schedule(Schedule())

    def _emit(self, *_args) -> None:
        if not self._loading:
            self.changed.emit()

    def _kind_changed(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self._emit()

    def set_schedule(self, schedule: Schedule) -> None:
        self._loading = True
        try:
            kinds = [k for _, k in self.KINDS]
            self.kind_combo.setCurrentIndex(kinds.index(schedule.kind) if schedule.kind in kinds else 0)
            self.pages.setCurrentIndex(self.kind_combo.currentIndex())
            # sensible values for the kinds not in use, so switching kind starts from something useful
            next_hour = QDateTime.currentDateTime().addSecs(3600)
            next_hour.setTime(QTime(next_hour.time().hour(), 0))
            self.at_edit.setDateTime(next_hour)
            self.time_edit.setTime(QTime(9, 0))
            days = set(range(7))
            self.minutes_spin.setValue(15)
            if schedule.kind == "once":
                at = parse_at(schedule.at)
                self.at_edit.setDateTime(QDateTime.fromString(at.strftime(AT_FORMAT), "yyyy-MM-dd HH:mm"))
            elif schedule.kind == "weekly":
                hour, minute = parse_clock(schedule.time)
                self.time_edit.setTime(QTime(hour, minute))
                days = set(schedule.days)
            elif schedule.kind == "interval":
                self.minutes_spin.setValue(schedule.minutes)
            for i, b in enumerate(self.day_buttons):
                b.setChecked(i in days)
        finally:
            self._loading = False

    def schedule(self) -> Schedule:
        """The schedule shown, validated; raises ``ValueError``."""
        kind = self.kind_combo.currentData()
        if kind == "once":
            s = Schedule("once", at=self.at_edit.dateTime().toString("yyyy-MM-dd HH:mm"))
        elif kind == "weekly":
            days = tuple(i for i, b in enumerate(self.day_buttons) if b.isChecked())
            s = Schedule("weekly", time=self.time_edit.time().toString("HH:mm"), days=days)
        elif kind == "interval":
            s = Schedule("interval", minutes=self.minutes_spin.value())
        else:
            s = Schedule()
        return s.validate()
