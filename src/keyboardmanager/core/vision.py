"""Seeing the screen: image and pixel matching for the vision steps (no Qt here).

Images are plain RGB byte buffers; the GUI captures the screen and decodes image
files (``UserInterface.grab_screen`` / ``load_image``), so this module stays free of
Qt and can be tested with hand-built images.

Matching is exact by default (fast: the template is searched as raw bytes), which
works when the template was captured from the same screen. A ``tolerance`` (0-255,
the allowed difference per colour channel) absorbs anti-aliasing and colour-profile
differences, but makes the search noticeably slower on large screens.

On high-resolution (Retina) screens the capture has more pixels than the mouse
coordinate system; ``Screen.scale`` says how many capture pixels one mouse step is,
and found positions are converted back with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Image:
    """An RGB image: 3 bytes per pixel, rows top to bottom."""

    width: int
    height: int
    pixels: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError("an image needs at least one pixel")
        if len(self.pixels) != self.width * self.height * 3:
            raise ValueError(
                f"expected {self.width * self.height * 3} bytes for {self.width}x{self.height}, "
                f"got {len(self.pixels)}"
            )

    @classmethod
    def build(cls, rows: list[list[tuple[int, int, int]]]) -> "Image":
        """An image from rows of (r, g, b) tuples (for tests and tools)."""
        data = bytearray()
        for row in rows:
            for r, g, b in row:
                data += bytes((r, g, b))
        return cls(len(rows[0]), len(rows), bytes(data))

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"({x}, {y}) is outside the {self.width}x{self.height} image")
        i = (y * self.width + x) * 3
        return self.pixels[i], self.pixels[i + 1], self.pixels[i + 2]

    def row(self, y: int, x: int = 0, width: int | None = None) -> bytes:
        start = (y * self.width + x) * 3
        return self.pixels[start:start + (self.width - x if width is None else width) * 3]


@dataclass(frozen=True)
class Screen(Image):
    """A screen capture; ``scale`` = capture pixels per mouse-coordinate step."""

    scale: float = 1.0


@dataclass(frozen=True)
class Match:
    """Where a template was found, in capture pixels (left/top) and mouse coordinates."""

    left: int
    top: int
    width: int
    height: int
    scale: float = 1.0

    @property
    def center(self) -> tuple[int, int]:
        """The middle of the match, in mouse coordinates."""
        return (round((self.left + self.width / 2) / self.scale),
                round((self.top + self.height / 2) / self.scale))


def find(screen: Image, template: Image, tolerance: int = 0) -> Match | None:
    """The first place (top-left first) where ``template`` appears on ``screen``."""
    if not 0 <= tolerance <= 255:
        raise ValueError("the tolerance must be between 0 and 255")
    if template.width > screen.width or template.height > screen.height:
        return None
    at = _find_exact(screen, template) if tolerance == 0 else _find_tolerant(screen, template, tolerance)
    if at is None:
        return None
    scale = screen.scale if isinstance(screen, Screen) else 1.0
    return Match(at[0], at[1], template.width, template.height, scale)


def _find_exact(screen: Image, template: Image) -> tuple[int, int] | None:
    """Byte-exact search: the first template row via bytes.find, then the others."""
    first = template.row(0)
    row_bytes = screen.width * 3
    for y in range(screen.height - template.height + 1):
        base = y * row_bytes
        end = base + row_bytes  # a match may not wrap into the next row
        offset = screen.pixels.find(first, base, end)
        while offset != -1:
            extra = offset - base
            if extra % 3 == 0:  # aligned to a pixel, not in the middle of one
                x = extra // 3
                if x + template.width <= screen.width and _rows_equal(screen, template, x, y):
                    return x, y
            offset = screen.pixels.find(first, offset + 1, end)
    return None


def _rows_equal(screen: Image, template: Image, x: int, y: int) -> bool:
    return all(screen.row(y + j, x, template.width) == template.row(j) for j in range(1, template.height))


def _find_tolerant(screen: Image, template: Image, tolerance: int) -> tuple[int, int] | None:
    """Every channel of every pixel may differ by up to ``tolerance``. Slower: the
    first template pixel filters candidates, full rows are only compared after it."""
    t0_r, t0_g, t0_b = template.pixel(0, 0)
    pixels, width = screen.pixels, screen.width
    rows = [template.row(j) for j in range(template.height)]
    for y in range(screen.height - template.height + 1):
        base = y * width * 3
        for x in range(width - template.width + 1):
            i = base + x * 3
            if (abs(pixels[i] - t0_r) > tolerance or abs(pixels[i + 1] - t0_g) > tolerance
                    or abs(pixels[i + 2] - t0_b) > tolerance):
                continue
            if all(_row_close(screen.row(y + j, x, template.width), rows[j], tolerance)
                   for j in range(template.height)):
                return x, y
    return None


def _row_close(a: bytes, b: bytes, tolerance: int) -> bool:
    for va, vb in zip(a, b, strict=True):
        if abs(va - vb) > tolerance:
            return False
    return True


# ------------------------------------------------------------------- colours
_COLOR = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def parse_color(text: str) -> tuple[int, int, int]:
    """``#rrggbb`` (or ``#rgb``) as (r, g, b); raises ``ValueError``."""
    m = _COLOR.match(text.strip())
    if not m:
        raise ValueError(f"{text!r} is not a colour like #1a2b3c")
    hexes = m.group(1)
    if len(hexes) == 3:
        hexes = "".join(c * 2 for c in hexes)
    return int(hexes[0:2], 16), int(hexes[2:4], 16), int(hexes[4:6], 16)


def format_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def color_close(a: tuple[int, int, int], b: tuple[int, int, int], tolerance: int = 0) -> bool:
    return all(abs(va - vb) <= tolerance for va, vb in zip(a, b, strict=True))
