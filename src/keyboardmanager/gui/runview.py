"""Run tab: the live execution view, the debugger controls and the run queue."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Action, Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.theme import set_tone

POLL_MS = 100
BREAKPOINT_MARK = "\u25cf "


def step_label(path: tuple) -> str:
    """A step path as the user reads it: (2, "children", 0) -> "3.1"."""
    parts, macro = [], ""
    for part in path:
        if isinstance(part, int):
            parts.append(str(part + 1))
        elif str(part).startswith("macro:"):
            macro = str(part)[len("macro:"):]
        elif part == "else_children":
            parts[-1:] = [parts[-1] + " else"] if parts else []
    label = ".".join(parts)
    return f'{label} in "{macro}"' if macro else label


class RunView(QWidget):
    def __init__(self, service: MacroService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._macro: Macro | None = None        # whose steps the tree shows
        self._items: dict[tuple, QTreeWidgetItem] = {}
        self._current: QTreeWidgetItem | None = None
        self._breakpoints: set[tuple] = set()

        self.title = QLabel("No macro is running.")
        font = QFont(self.title.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.title.setFont(font)
        self.details = QLabel()
        set_tone(self.details, "secondary")
        self.step_label = QLabel()
        self.step_label.setWordWrap(True)

        # debugger / run controls
        self.continue_btn = QPushButton("Continue")
        self.continue_btn.setToolTip("Run to the next breakpoint (or resume a paused macro)")
        self.step_btn = QPushButton("Step")
        self.step_btn.setToolTip("Run the next step, then stop again")
        self.pause_btn = QPushButton("Pause")
        self.stop_btn = QPushButton("Stop")
        self.continue_btn.clicked.connect(self._continue)
        self.step_btn.clicked.connect(self.service.debug_step)
        self.pause_btn.clicked.connect(self.service.pause)
        self.stop_btn.clicked.connect(self.service.stop)
        controls = QHBoxLayout()
        for b in (self.continue_btn, self.step_btn, self.pause_btn, self.stop_btn):
            controls.addWidget(b)
        controls.addStretch(1)

        # the macro's steps, the current one highlighted
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.setIndentation(18)

        # what happened / what is there
        self.recent = QTableWidget(0, 3)
        self.recent.setHorizontalHeaderLabels(("At", "Step", "Result"))
        self.variables = QTableWidget(0, 2)
        self.variables.setHorizontalHeaderLabels(("Variable", "Value"))
        self.simulated = QListWidget()
        for table in (self.recent, self.variables):
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(table.columnCount() - 1, QHeaderView.ResizeMode.Stretch)
        self.recent.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.recent.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.panes = QTabWidget()
        self.panes.addTab(self.recent, "Recent steps")
        self.panes.addTab(self.variables, "Variables")
        self.panes.addTab(self.simulated, "Simulated input")

        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(self.panes)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)

        # the queue
        queue_box = QGroupBox("Queue (runs waiting for the current one to finish)")
        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(110)
        self.queue_list.itemSelectionChanged.connect(self._update_queue_buttons)
        self.remove_btn = QPushButton("Remove")
        self.clear_btn = QPushButton("Clear queue")
        self.remove_btn.clicked.connect(self._remove_queued)
        self.clear_btn.clicked.connect(self.service.clear_queue)
        queue_buttons = QVBoxLayout()
        queue_buttons.addWidget(self.remove_btn)
        queue_buttons.addWidget(self.clear_btn)
        queue_buttons.addStretch(1)
        queue_layout = QHBoxLayout(queue_box)
        queue_layout.addWidget(self.queue_list, 1)
        queue_layout.addLayout(queue_buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.details)
        layout.addLayout(controls)
        layout.addWidget(self.step_label)
        layout.addWidget(split, 1)
        layout.addWidget(queue_box)

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.refresh)
        self.show_queue(service.queue_snapshot())
        self.refresh()

    # ---------------------------------------------------------------- service
    def started(self, breakpoints: set[tuple] | None = None) -> None:
        """A run started (the main window passes a debug run's breakpoints)."""
        self._breakpoints = set(breakpoints or ())
        self._macro = None  # rebuild the tree for the new run
        self.refresh()
        self.timer.start()

    def finished(self, macro: Macro, status: str, message: str) -> None:
        self.refresh()  # shows the idle state: the run is over
        self.title.setText(f'"{macro.name}" finished: {status}')
        self.step_label.setText(message)
        final = self.service.last_run_state()
        if final is not None and final["macro"] == macro.name:  # what it ended with
            self.details.setText(f'Started by: {final["trigger"]}  |  {final["elapsed"]:.1f} s  |  '
                                 f'{final["steps_run"]} steps run')
            self._show_panes(final)

    def show_queue(self, queued: list[tuple[str, str]]) -> None:
        self.queue_list.clear()
        for number, (name, trigger) in enumerate(queued, 1):
            self.queue_list.addItem(f"{number}. {name}  ({trigger})")
        self.clear_btn.setEnabled(bool(queued))
        self._update_queue_buttons()

    def _update_queue_buttons(self) -> None:
        self.remove_btn.setEnabled(bool(self.queue_list.selectedItems()))

    def _remove_queued(self) -> None:
        rows = sorted({self.queue_list.row(i) for i in self.queue_list.selectedItems()}, reverse=True)
        for row in rows:
            self.service.remove_queued(row)

    def _continue(self) -> None:
        state = self.service.live_state()
        if state is not None and state["debugging"]:
            self.service.debug_continue()
        else:
            self.service.resume()

    # ---------------------------------------------------------------- refresh
    def refresh(self) -> None:
        state = self.service.live_state()
        macro = self.service.running_macro
        if state is None or macro is None:
            self.timer.stop()
            self._set_current(None)
            for b in (self.continue_btn, self.step_btn, self.pause_btn, self.stop_btn):
                b.setEnabled(False)
            if self._macro is None:
                self.title.setText("No macro is running.")
                self.details.setText("Start a macro, or debug one from the editor (Debug button).")
            else:
                self.details.setText("")
            return
        if macro is not self._macro:
            self._build_tree(macro)
        debugging, paused = state["debugging"], state["paused"]
        stopped_at = state.get("stopped_at")
        if debugging and stopped_at is not None and paused:
            status = f"stopped before step {step_label(stopped_at)}"
        elif paused:
            status = "paused"
        else:
            status = "running"
        self.title.setText(f'"{state["macro"]}" - {status}' + (" (debugging)" if debugging else ""))
        self.details.setText(
            f'Started by: {state["trigger"]}  |  {state["elapsed"]:.1f} s  |  {state["steps_run"]} steps run'
            + ("  |  input is simulated, nothing is sent" if state.get("simulated") is not None else "")
        )
        path = tuple(state["step"])
        self.step_label.setText(
            f"{'Next' if paused and debugging else 'Current'} step {step_label(path)}: {state['step_text']}"
            if path else "Starting..."
        )
        self._set_current(self._items.get(path))
        self.continue_btn.setEnabled(paused)
        self.step_btn.setEnabled(debugging and paused)
        self.pause_btn.setEnabled(not paused)
        self.stop_btn.setEnabled(True)
        self._show_panes(state)
        if not self.timer.isActive():
            self.timer.start()

    def _show_panes(self, state: dict) -> None:
        self._show_recent(state["recent"])
        self._show_variables(state["variables"])
        simulated = state.get("simulated")
        self.panes.setTabEnabled(2, simulated is not None)
        if simulated is None:
            self.simulated.clear()
        elif [self.simulated.item(i).text() for i in range(self.simulated.count())] != (
                lines := [f"{t:6.2f} s  {text}" for t, text in simulated]):
            self.simulated.clear()
            self.simulated.addItems(lines)
            self.simulated.scrollToBottom()

    def _show_recent(self, recent: list) -> None:
        self.recent.setRowCount(len(recent))
        for row, entry in enumerate(recent):
            values = (f"{entry.offset_ms / 1000:.2f} s", "  " * entry.depth + entry.description,
                      entry.status + (f" - {entry.detail}" if entry.detail else ""))
            for col, value in enumerate(values):
                self.recent.setItem(row, col, QTableWidgetItem(value))
        if recent:
            self.recent.scrollToBottom()

    def _show_variables(self, variables: dict) -> None:
        self.variables.setRowCount(len(variables))
        for row, (name, value) in enumerate(sorted(variables.items())):
            self.variables.setItem(row, 0, QTableWidgetItem(name))
            self.variables.setItem(row, 1, QTableWidgetItem(str(value)))

    # ------------------------------------------------------------------- tree
    def _build_tree(self, macro: Macro) -> None:
        self._macro = macro
        self._items = {}
        self._current = None
        self.tree.clear()

        def add(parent, actions: list[Action], prefix: tuple) -> None:
            for index, action in enumerate(actions):
                path = (*prefix, index)
                mark = BREAKPOINT_MARK if path in self._breakpoints else ""
                item = QTreeWidgetItem([f"{step_label(path)}  {mark}{catalog.describe(action)}"])
                if mark:
                    item.setForeground(0, QBrush(QColor("#c62828")))
                self._items[path] = item
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                if catalog.spec_for(action.type).has_else:
                    for branch, label in (("children", "Then"), ("else_children", "Else")):
                        head = QTreeWidgetItem([label])
                        item.addChild(head)
                        add(head, getattr(action, branch), (*path, branch))
                else:
                    add(item, action.children, (*path, "children"))

        add(None, macro.actions, ())
        self.tree.expandAll()

    def _set_current(self, item: QTreeWidgetItem | None) -> None:
        if item is self._current:
            return
        if self._current is not None:
            self._current.setBackground(0, QBrush())
        self._current = item
        if item is not None:
            item.setBackground(0, QBrush(self.current_color()))
            self.tree.scrollToItem(item)

    def current_color(self) -> QColor:
        """The step running now / the debugger stopped before: the accent, tinted."""
        color = QColor(self.palette().color(QPalette.ColorRole.Highlight))
        color.setAlpha(110)
        return color

    def theme_changed(self) -> None:
        if self._current is not None:
            self._current.setBackground(0, QBrush(self.current_color()))
