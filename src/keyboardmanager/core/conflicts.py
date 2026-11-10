"""Why a hotkey or abbreviation cannot be used: shared by the editor (when saving)
and the pre-flight check."""

from __future__ import annotations

from keyboardmanager.core import keys
from keyboardmanager.core.expander import abbreviations_conflict
from keyboardmanager.core.models import Macro


def hotkey_conflict(macro: Macro, others: list[Macro], reserved: dict[str, str]) -> str | None:
    """Explain why ``macro.hotkey`` cannot be used, or return ``None`` if it is fine.

    ``reserved`` maps the application's own hotkeys to their purpose (``stop``, ``pause``).
    """
    from keyboardmanager.core.io import scopes_conflict

    if not macro.hotkey:
        return None
    chords = macro.hotkey.split()
    for hotkey, purpose in reserved.items():
        if hotkey and keys.normalize_hotkey(hotkey) in chords:
            return f"{keys.normalize_hotkey(hotkey)} is reserved as the {purpose} hotkey."
    for other in others:
        if other.id == macro.id or not other.hotkey or not scopes_conflict(other, macro):
            continue
        if other.hotkey == macro.hotkey:
            return f'{macro.hotkey} is already used by "{other.name}" for the same scope.'
        if keys.sequence_prefix_conflict(other.hotkey, macro.hotkey):
            # one would always fire (or swallow the chord) before the other can
            return (
                f'{macro.hotkey} overlaps with "{other.name}" ({other.hotkey}): '
                "one is how the other starts."
            )
    return None


def abbreviation_conflict(macro: Macro, others: list[Macro]) -> str | None:
    """Explain why ``macro.abbreviation`` cannot be used, or return ``None``."""
    from keyboardmanager.core.io import scopes_conflict

    if not macro.abbreviation:
        return None
    for other in others:
        if other.id == macro.id or not other.abbreviation or not scopes_conflict(other, macro):
            continue
        if abbreviations_conflict(other.abbreviation, macro.abbreviation):
            same = other.abbreviation == macro.abbreviation
            return (
                f'"{macro.abbreviation}" is already used by "{other.name}".' if same else
                f'"{macro.abbreviation}" overlaps with "{other.name}" ("{other.abbreviation}"): '
                "typing one would always trigger the other first."
            )
    return None
