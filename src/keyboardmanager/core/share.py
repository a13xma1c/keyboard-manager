"""Share codes: macros as one line of text, to paste into a chat, mail or ticket.

A share code is the normal export document (see ``core.io``), compressed and
base64-encoded behind a recognisable prefix::

    KMSHARE1:eNqrVkrLz1eyUkpKLFKqBQAdnQSL

Importing a code goes through the normal import (validation, renaming on name
clashes, hotkeys that clash are dropped), so a code can never overwrite anything.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib

from keyboardmanager.core.io import export_to_dict
from keyboardmanager.core.models import Macro

PREFIX = "KMSHARE1:"
MAX_CODE_CHARS = 2_000_000      # a pasted code longer than this is refused
MAX_DOCUMENT_BYTES = 10_000_000  # and so is one that unpacks to more than this


class ShareError(ValueError):
    pass


def encode(macros: list[Macro]) -> str:
    if not macros:
        raise ShareError("choose at least one macro to share")
    document = json.dumps(export_to_dict(macros), ensure_ascii=False, separators=(",", ":"))
    packed = zlib.compress(document.encode("utf-8"), 9)
    return PREFIX + base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


def decode(code: str) -> str:
    """The export document (JSON text) inside a share code; raises ``ShareError``."""
    text = "".join(code.split())  # codes survive line wrapping in mails and chats
    if not text.startswith(PREFIX):
        raise ShareError("this is not a Keyboard Manager share code (it starts with KMSHARE1:)")
    if len(text) > MAX_CODE_CHARS:
        raise ShareError("the share code is too long")
    body = text[len(PREFIX):]
    try:
        packed = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ShareError("the share code is damaged (not base64)") from None
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(packed, MAX_DOCUMENT_BYTES)
    except zlib.error:
        raise ShareError("the share code is damaged (incomplete?)") from None
    if inflater.unconsumed_tail:
        raise ShareError("the share code unpacks to too much data")
    if not inflater.eof:
        raise ShareError("the share code is incomplete (was it cut off?)")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ShareError("the share code is damaged") from None


def looks_like_code(text: str) -> bool:
    return "".join(text.split()).startswith(PREFIX)
