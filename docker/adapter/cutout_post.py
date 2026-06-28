"""Remove cream ground from generated illustrations (Pi pipeline cutout.py step)."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from io import BytesIO
from pathlib import Path

import numpy as np

log = logging.getLogger("cutout")

CUTOUT_ENABLED = os.environ.get("ILLUSTRATION_CUTOUT", "1").lower() in ("1", "true", "yes")
CUTOUT_MODEL = os.environ.get("CUTOUT_MODEL", "u2net")
CUTOUT_MARGIN = float(os.environ.get("CUTOUT_MARGIN", "0.012"))
CUTOUT_MODE = os.environ.get("CUTOUT_MODE", "hybrid").lower()
CUTOUT_CREAM_TOLERANCE = float(os.environ.get("CUTOUT_CREAM_TOLERANCE", "38"))
CUTOUT_CREAM_FRINGE = float(os.environ.get("CUTOUT_CREAM_FRINGE", "32"))
CUTOUT_ALPHA_THRESHOLD = int(os.environ.get("CUTOUT_ALPHA_THRESHOLD", "145"))
CUTOUT_BELLY_HOLE_MIN = int(os.environ.get("CUTOUT_BELLY_HOLE_MIN", "72"))
CUTOUT_FRINGE_PASSES = int(os.environ.get("CUTOUT_FRINGE_PASSES", "3"))

_session = None
_session_lock = threading.Lock()


def _flood_from_edges(mask: np.ndarray) -> np.ndarray:
    """True for mask pixels reachable from any image border through mask."""
    h, w = mask.shape
    reached = np.zeros((h, w), dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    for x in range(w):
        if mask[0, x]:
            queue.append((0, x))
        if mask[h - 1, x]:
            queue.append((h - 1, x))
    for y in range(h):
        if mask[y, 0]:
            queue.append((y, 0))
        if mask[y, w - 1]:
            queue.append((y, w - 1))
    while queue:
        y, x = queue.popleft()
        if y < 0 or y >= h or x < 0 or x >= w or reached[y, x] or not mask[y, x]:
            continue
        reached[y, x] = True
        queue.extend(((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)))
    return reached


def _cream_color(rgb: np.ndarray) -> np.ndarray:
    h, w = rgb.shape[:2]
    patch = max(4, min(h, w) // 20)
    corners = np.vstack(
        [
            rgb[:patch, :patch].reshape(-1, 3),
            rgb[:patch, -patch:].reshape(-1, 3),
            rgb[-patch:, :patch].reshape(-1, 3),
            rgb[-patch:, -patch:].reshape(-1, 3),
        ]
    )
    return np.median(corners, axis=0).astype(np.float32)


def _cream_distance(rgb: np.ndarray, cream: np.ndarray) -> np.ndarray:
    return np.sqrt(((rgb.astype(np.float32) - cream) ** 2).sum(axis=2))


def _fill_holes(foreground: np.ndarray) -> np.ndarray:
    """Fill interior holes in a boolean foreground mask."""
    bg = ~foreground
    exterior_bg = _flood_from_edges(bg)
    holes = bg & ~exterior_bg
    return foreground | holes


def _label_components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    h, w = mask.shape
    visited = np.zeros((h, w), dtype=bool)
    components: list[list[tuple[int, int]]] = []
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or visited[y, x]:
                continue
            comp: list[tuple[int, int]] = []
            queue: deque[tuple[int, int]] = deque([(y, x)])
            while queue:
                cy, cx = queue.popleft()
                if cy < 0 or cy >= h or cx < 0 or cx >= w or visited[cy, cx] or not mask[cy, cx]:
                    continue
                visited[cy, cx] = True
                comp.append((cy, cx))
                queue.extend(((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)))
            if comp:
                components.append(comp)
    return components


def _strip_cream_fringe(fg: np.ndarray, fringe_cream: np.ndarray, ml_alpha: np.ndarray) -> np.ndarray:
    """Peel paper-coloured pixels sitting on the bird silhouette edge."""
    out = fg.copy()
    weak = ml_alpha < 215
    out &= ~(fringe_cream & weak)
    for _ in range(max(1, CUTOUT_FRINGE_PASSES)):
        bg = ~out
        touch = np.zeros_like(out)
        touch[1:, :] |= bg[:-1, :]
        touch[:-1, :] |= bg[1:, :]
        touch[:, 1:] |= bg[:, :-1]
        touch[:, :-1] |= bg[:, 1:]
        peel = out & fringe_cream & touch
        if not peel.any():
            break
        out &= ~peel
    return out


def _repair_belly_holes(
    fg: np.ndarray,
    ml_fg: np.ndarray,
    cream_like: np.ndarray,
) -> np.ndarray:
    """Fill rembg belly holes but leave small cream gaps (e.g. between toes)."""
    ml_filled = _fill_holes(ml_fg)
    holes = ml_filled & ~ml_fg
    if not holes.any():
        return fg
    out = fg.copy()
    for comp in _label_components(holes):
        area = len(comp)
        ys = np.fromiter((p[0] for p in comp), dtype=np.intp)
        xs = np.fromiter((p[1] for p in comp), dtype=np.intp)
        mostly_cream = cream_like[ys, xs].mean() > 0.55
        if mostly_cream and area < CUTOUT_BELLY_HOLE_MIN:
            continue
        if mostly_cream and area < CUTOUT_BELLY_HOLE_MIN * 4:
            continue
        out[ys, xs] = True
    return out


def _compose_alpha(rgb: np.ndarray, ml_alpha: np.ndarray) -> np.ndarray:
    """Merge ML matting with cream removal; tight edge, no belly or toe-gap holes."""
    cream = _cream_color(rgb)
    cream_dist = _cream_distance(rgb, cream)
    cream_like = cream_dist <= CUTOUT_CREAM_TOLERANCE
    fringe_cream = cream_dist <= CUTOUT_CREAM_FRINGE
    ml_fg = ml_alpha >= CUTOUT_ALPHA_THRESHOLD

    if CUTOUT_MODE == "rembg":
        fg = ml_fg
    else:
        edge_cream = _flood_from_edges(cream_like)
        fg = ml_fg.copy()
        fg[edge_cream] = False
        fg = _strip_cream_fringe(fg, fringe_cream, ml_alpha)
        fg = _repair_belly_holes(fg, ml_fg, cream_like)

    out = np.zeros(ml_alpha.shape, dtype=np.uint8)
    out[fg] = 255
    return out


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            from rembg import new_session

            log.info("Loading rembg model %s (first run may download weights)", CUTOUT_MODEL)
            _session = new_session(CUTOUT_MODEL)
            log.info("rembg model %s ready", CUTOUT_MODEL)
        return _session


def preload_session() -> None:
    """Load rembg at startup so the first bird generation does not hang silently."""
    if not CUTOUT_ENABLED:
        return
    try:
        _get_session()
    except Exception as exc:
        log.warning("rembg preload failed: %s", exc)


def cutout_png(raw: bytes) -> bytes | None:
    """Matte the bird onto transparency and crop. Returns PNG bytes or None on failure."""
    if not CUTOUT_ENABLED:
        return raw
    try:
        from PIL import Image
        from rembg import remove
    except ImportError:
        log.warning("rembg/Pillow not installed — skipping cutout")
        return raw

    try:
        im = Image.open(BytesIO(raw)).convert("RGB")
        rgb = np.array(im)
        session = _get_session()
        cut = remove(rgb, session=session)
        ml_alpha = np.array(cut.getchannel("A"))
        alpha = _compose_alpha(rgb, ml_alpha)
        cut.putalpha(Image.fromarray(alpha, mode="L"))
        bbox = cut.getchannel("A").getbbox()
        if bbox:
            pad = round(CUTOUT_MARGIN * max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
            x0, y0 = max(0, bbox[0] - pad), max(0, bbox[1] - pad)
            x1, y1 = min(cut.width, bbox[2] + pad), min(cut.height, bbox[3] + pad)
            cut = cut.crop((x0, y0, x1, y1))
        out = BytesIO()
        cut.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as exc:
        log.warning("Cutout failed: %s", exc)
        return raw


def cutout_file(path: Path) -> bool:
    data = path.read_bytes()
    cut = cutout_png(data)
    if not cut or len(cut) < 1024:
        return False
    path.write_bytes(cut)
    return True
