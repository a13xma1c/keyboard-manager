"""Folders, tags and favorites: how macros are organised (no Qt)."""

from __future__ import annotations

import json
import threading
from typing import Callable, Iterable

FOLDERS_SETTING = "folders"  # folders that exist even while empty, as JSON
MAX_TAG_LENGTH = 32
MIN_SPEED, MAX_SPEED = 0.1, 10.0


# ------------------------------------------------------------------- folders
def normalize_folder(path: str) -> str:
    """``" Work / Email/ "`` -> ``"Work/Email"``; ``""`` is the top level."""
    parts = [p.strip() for p in path.replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    for part in parts:
        if len(part) > 60:
            raise ValueError("a folder name may have at most 60 characters")
    return "/".join(parts)


def parent_folder(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def folder_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def ancestors(path: str) -> list[str]:
    """``"a/b/c"`` -> ``["a", "a/b", "a/b/c"]``."""
    parts = path.split("/") if path else []
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def is_inside(path: str, folder: str) -> bool:
    """``path`` is ``folder`` itself or somewhere below it."""
    return path == folder or path.startswith(folder + "/")


def moved_path(path: str, old: str, new: str) -> str:
    """Where ``path`` ends up when folder ``old`` becomes ``new``."""
    return new + path[len(old):] if is_inside(path, old) else path


class FolderStore:
    """Folders the user created that may be empty (macros' folders always exist)."""

    def __init__(self, load: Callable[[], str], save: Callable[[str], None]) -> None:
        self._load, self._save = load, save
        self._lock = threading.Lock()

    def all(self, macro_folders: Iterable[str] = ()) -> list[str]:
        """Every folder, including parents and the folders of ``macro_folders``."""
        with self._lock:
            paths = set(self._read()) | set(macro_folders)
        return sorted({a for p in paths if p for a in ancestors(p)}, key=str.lower)

    def add(self, path: str) -> str:
        path = normalize_folder(path)
        if not path:
            raise ValueError("please give the folder a name")
        with self._lock:
            self._write(sorted(set(self._read()) | {path}))
        return path

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            self._write(sorted({moved_path(p, old, new) for p in self._read()}))

    def remove(self, path: str) -> None:
        """Forget ``path`` and everything below it."""
        with self._lock:
            self._write([p for p in self._read() if not is_inside(p, path)])

    def _read(self) -> list[str]:
        try:
            data = json.loads(self._load() or "[]")
        except ValueError:
            return []
        return [normalize_folder(p) for p in data if isinstance(p, str) and normalize_folder(p)]

    def _write(self, paths: list[str]) -> None:
        self._save(json.dumps(paths, ensure_ascii=False))


# ---------------------------------------------------------------------- tags
def normalize_tags(tags: Iterable[str] | str) -> list[str]:
    """Tags are lower case, without a leading ``#``, unique, in the given order.
    A string is split on commas (what the editor's field holds)."""
    if isinstance(tags, str):
        tags = tags.split(",")
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("tags must be text")
        tag = " ".join(tag.strip().lstrip("#").split()).lower()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"a tag may have at most {MAX_TAG_LENGTH} characters")
        if tag not in out:
            out.append(tag)
    return out


def validate_speed(speed: object) -> float:
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ValueError("the speed must be a number")
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ValueError(f"the speed must be between {MIN_SPEED:g}x and {MAX_SPEED:g}x")
    return float(speed)
