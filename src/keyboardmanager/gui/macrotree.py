"""The macro list: folders (nested), favorites first, filtering by text and tags."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QStyle, QTreeWidget, QTreeWidgetItem, QWidget

from keyboardmanager.core.models import Macro
from keyboardmanager.core.organize import ancestors, folder_name, parent_folder
from keyboardmanager.core.profiles import in_profile

ROLE_ID = Qt.ItemDataRole.UserRole          # macro items: the macro id
ROLE_FOLDER = Qt.ItemDataRole.UserRole + 1  # folder items: the folder path
FAVORITES = "favorites"                     # filter value: only favorites


def macro_label(macro: Macro) -> str:
    parts = [("\u2605 " if macro.favorite else "") + macro.name]
    if macro.hotkey:
        parts.append(f"[{macro.hotkey}]")
    if macro.abbreviation:
        parts.append(f'"{macro.abbreviation}"')
    if macro.schedule.active:
        parts.append("(scheduled)")
    if macro.event:
        parts.append("(on event)")
    if not macro.enabled:
        parts.append("(disabled)")
    return "  ".join(parts)


def matches(macro: Macro, needle: str, filter_: str) -> bool:
    """``needle`` searches names, hotkeys, abbreviations and tags ("#tag" = that tag);
    ``filter_`` is ``""`` (all), ``FAVORITES`` or a tag."""
    if filter_ == FAVORITES and not macro.favorite:
        return False
    if filter_ and filter_ != FAVORITES and filter_ not in macro.tags:
        return False
    for word in needle.lower().split():
        if word.startswith("#"):
            if not any(tag.startswith(word[1:]) for tag in macro.tags):
                return False
        elif word not in "\n".join([macro.name, macro.hotkey, macro.abbreviation, *macro.tags]).lower():
            return False
    return True


class MacroTree(QTreeWidget):
    moved = Signal(list, str)  # macro ids dropped onto a folder ("" = top level)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setIndentation(16)
        self._collapsed: set[str] = set()  # folders the user closed (kept across refreshes)
        self.itemCollapsed.connect(self._remember_collapsed)
        self.itemExpanded.connect(self._remember_expanded)

    # ------------------------------------------------------------- building
    def populate(self, macros: list[Macro], folders: list[str], needle: str = "", filter_: str = "",
                 current_id: int | None = None, selected: set[int] | None = None,
                 active_profile: str = "") -> None:
        """Rebuild the tree, keeping the current / selected macros and open folders."""
        selected = selected or set()
        filtering = bool(needle.strip() or filter_)
        shown = [m for m in macros if matches(m, needle, filter_)]
        self.blockSignals(True)
        self.clear()
        folder_items: dict[str, QTreeWidgetItem] = {}
        used = {a for m in shown for a in ancestors(m.folder)}
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        for path in folders:
            if filtering and path not in used:
                continue  # while searching, only folders with results
            parent = folder_items.get(parent_folder(path))
            item = QTreeWidgetItem([folder_name(path)])
            item.setIcon(0, icon)
            item.setData(0, ROLE_FOLDER, path)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled)
            (parent.addChild(item) if parent is not None else self.addTopLevelItem(item))
            folder_items[path] = item
        current_item = None
        for macro in sorted(shown, key=lambda m: (not m.favorite, m.name.lower())):
            outside = not in_profile(macro.profiles, active_profile)
            item = QTreeWidgetItem([macro_label(macro) + ("  (other profile)" if outside else "")])
            item.setData(0, ROLE_ID, macro.id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
            if outside:
                item.setForeground(0, Qt.GlobalColor.gray)
            tip = [macro.scope_label]
            if macro.profiles:
                tip.append("Profiles: " + ", ".join(macro.profiles))
            if macro.layer:
                tip.append(f"Layer: {macro.layer}")
            if macro.folder:
                tip.append(f"Folder: {macro.folder}")
            if macro.tags:
                tip.append("Tags: " + ", ".join(f"#{t}" for t in macro.tags))
            if macro.schedule.active:
                tip.append(f"Schedule: {macro.schedule.describe()}")
            if macro.event:
                from keyboardmanager.core.events import describe_event

                tip.append(f"Event: {describe_event(macro.event)}")
            tip.append(macro.description)
            item.setToolTip(0, "\n".join(t for t in tip if t).strip())
            parent = folder_items.get(macro.folder)
            (parent.addChild(item) if parent is not None else self.addTopLevelItem(item))
            if macro.id == current_id:
                current_item = item
            if macro.id in selected or macro.id == current_id:
                item.setSelected(True)
        for path, item in folder_items.items():
            count = sum(1 for m in shown if m.folder == path or m.folder.startswith(path + "/"))
            item.setText(0, f"{folder_name(path)}  ({count})" if count else folder_name(path))
            item.setExpanded(filtering or path not in self._collapsed)
        if current_item is not None:
            self.setCurrentItem(current_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.blockSignals(False)

    def _remember_collapsed(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, ROLE_FOLDER)
        if path is not None:
            self._collapsed.add(path)

    def _remember_expanded(self, item: QTreeWidgetItem) -> None:
        self._collapsed.discard(item.data(0, ROLE_FOLDER))

    # -------------------------------------------------------------- queries
    def macro_items(self) -> list[QTreeWidgetItem]:
        """Every macro item, in the order shown."""
        out: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            if item.data(0, ROLE_ID) is not None:
                out.append(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        return out

    def folder_items(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            if item.data(0, ROLE_FOLDER) is not None:
                out.append(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.topLevelItemCount()):
            walk(self.topLevelItem(i))
        return out

    def item_for(self, macro_id: int) -> QTreeWidgetItem | None:
        return next((i for i in self.macro_items() if i.data(0, ROLE_ID) == macro_id), None)

    def selected_ids(self) -> list[int]:
        return [i.data(0, ROLE_ID) for i in self.macro_items() if i.isSelected()]

    def selected_folder(self) -> str | None:
        """The folder to work in: a selected folder, or the folder of the current macro."""
        item = self.currentItem()
        if item is None:
            return None
        if item.data(0, ROLE_FOLDER) is not None:
            return item.data(0, ROLE_FOLDER)
        parent = item.parent()
        return parent.data(0, ROLE_FOLDER) if parent is not None else ""

    # ---------------------------------------------------------- drag & drop
    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        """Dropping macros on a folder (or a macro in it) moves them there; the main
        window saves that and rebuilds the tree, so Qt's own move is not used."""
        target = self.itemAt(event.position().toPoint())
        if target is None:
            folder = ""
        elif target.data(0, ROLE_FOLDER) is not None:
            folder = target.data(0, ROLE_FOLDER)
        else:
            parent = target.parent()
            folder = parent.data(0, ROLE_FOLDER) if parent is not None else ""
        ids = self.selected_ids()
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if ids:
            self.moved.emit(ids, folder)

