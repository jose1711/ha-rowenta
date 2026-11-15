"""Render the gyro map (256×256 grid, cleaning start at 128,128) to PNG."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

ORIGIN = 128
GRID = range(256)  # valid coordinates of the gyro map grid
PADDING = 2
MAX_SIZE = 800
MIN_CELL, MAX_CELL = 4, 24

# Point types as in the app (MapPointType)
CLEANED = {1, 6, 11}
PATH = {3, 8, 12}
OBSTACLE = {2, 7}
WALL = {4, 9}
CLIFF = {5, 10}

COLORS = {
    "cleaned": (130, 190, 255, 255),
    "path": (60, 130, 230, 255),
    "obstacle": (230, 80, 80, 255),
    "wall": (90, 90, 90, 255),
    "cliff": (170, 60, 170, 255),
    "other": (170, 170, 170, 255),
    "start": (40, 170, 70, 255),
    "robot": (255, 190, 0, 255),
}


def _color(kind: int) -> tuple[int, int, int, int]:
    if kind in CLEANED:
        return COLORS["cleaned"]
    if kind in PATH:
        return COLORS["path"]
    if kind in OBSTACLE:
        return COLORS["obstacle"]
    if kind in WALL:
        return COLORS["wall"]
    if kind in CLIFF:
        return COLORS["cliff"]
    return COLORS["other"]


def render_map(points: list[dict[str, Any]]) -> bytes:
    """Return a PNG. Robot = last position point (CURRENT type), green ring = cleaning start."""
    # Only points inside the 256×256 grid: a single bogus coordinate from the cloud
    # would otherwise blow the canvas up to gigabytes.
    pts = [
        p
        for p in points
        if isinstance(p, dict)
        and isinstance(p.get("x"), int)
        and isinstance(p.get("y"), int)
        and p["x"] in GRID
        and p["y"] in GRID
    ]
    pts.sort(key=lambda p: p.get("index", 0))
    xs = [p["x"] for p in pts] + [ORIGIN]
    ys = [p["y"] for p in pts] + [ORIGIN]
    min_x, max_x = min(xs) - PADDING, max(xs) + PADDING
    min_y, max_y = min(ys) - PADDING, max(ys) + PADDING
    width, height = max_x - min_x + 1, max_y - min_y + 1
    cell = max(MIN_CELL, min(MAX_CELL, MAX_SIZE // max(width, height)))
    gap = 1 if cell >= 8 else 0

    img = Image.new("RGBA", (width * cell, height * cell), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def box(x: int, y: int) -> tuple[int, int, int, int]:
        left, top = (x - min_x) * cell, (max_y - y) * cell
        return left, top, left + cell - 1 - gap, top + cell - 1 - gap

    # A later point overwrites an earlier one (same as the app).
    cells: dict[tuple[int, int], int] = {}
    for p in pts:
        cells[(p["x"], p["y"])] = int(p.get("type", -1))
    for (x, y), kind in cells.items():
        draw.rectangle(box(x, y), fill=_color(kind))

    inset = max(1, cell // 6)
    left, top, right, bottom = box(ORIGIN, ORIGIN)
    draw.ellipse((left + inset, top + inset, right - inset, bottom - inset), outline=COLORS["start"], width=max(2, cell // 6))

    if last := next((p for p in reversed(pts) if p.get("type") in PATH), None):
        left, top, right, bottom = box(last["x"], last["y"])
        draw.ellipse((left - 2, top - 2, right + 2, bottom + 2), fill=COLORS["robot"], outline=(40, 40, 40, 255), width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
