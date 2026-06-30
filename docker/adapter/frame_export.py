"""Export a 3840×2160 JPEG poster for Samsung Frame / Home Assistant."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

log = logging.getLogger("frame_export")

FRAME_EXPORT_ENABLED = os.environ.get("FRAME_EXPORT_ENABLED", "1").lower() in ("1", "true", "yes")
FRAME_EXPORT_DIR = Path(os.environ.get("FRAME_EXPORT_DIR", "/data/frame-export"))
FRAME_EXPORT_HOURS = int(os.environ.get("FRAME_EXPORT_HOURS", "0"))
FRAME_EXPORT_INTERVAL = max(60, int(os.environ.get("FRAME_EXPORT_INTERVAL", "300")))
FRAME_EXPORT_MAX_BIRDS = max(1, int(os.environ.get("FRAME_EXPORT_MAX_BIRDS", "28")))
FRAME_EXPORT_POSE = max(1, int(os.environ.get("FRAME_EXPORT_POSE", "1")))
FRAME_EXPORT_GAP = max(8, int(os.environ.get("FRAME_EXPORT_GAP", "56")))
FRAME_EXPORT_MARGIN = max(16, int(os.environ.get("FRAME_EXPORT_MARGIN", "72")))
FRAME_W = int(os.environ.get("FRAME_EXPORT_WIDTH", "3840"))
FRAME_H = int(os.environ.get("FRAME_EXPORT_HEIGHT", "2160"))
FRAME_BG = tuple(
    int(x) for x in os.environ.get("FRAME_EXPORT_BG", "252,252,251").split(",")[:3]
)

OUTPUT_NAME = "current.jpg"
STATUS_NAME = "current.json"
_lock = threading.Lock()
_export_timer: threading.Timer | None = None


def _window_label(hours: int) -> str:
    if hours == 0:
        return "today"
    if hours >= 1000000:
        return "all time"
    return f"{hours}h"


def _status_path() -> Path:
    return FRAME_EXPORT_DIR / STATUS_NAME


def _output_path() -> Path:
    return FRAME_EXPORT_DIR / OUTPUT_NAME


def frame_jpg_path() -> Path:
    return _output_path()


def read_status() -> dict[str, Any]:
    path = _status_path()
    if not path.is_file():
        return {"ready": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ready": False}


def _write_status(payload: dict[str, Any]) -> None:
    FRAME_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    _status_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_cutout(path: Path, max_side: int) -> Image.Image | None:
    try:
        im = Image.open(path).convert("RGBA")
    except OSError as exc:
        log.debug("skip cutout %s: %s", path.name, exc)
        return None
    w, h = im.size
    if not w or not h:
        return None
    scale = min(max_side / w, max_side / h, 1.0)
    if scale < 0.99:
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    return im


def _boxes_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int], gap: int) -> bool:
    return not (
        a[2] + gap <= b[0]
        or a[0] >= b[2] + gap
        or a[3] + gap <= b[1]
        or a[1] >= b[3] + gap
    )


def _find_open_slot(
    w: int,
    h: int,
    placed: list[tuple[int, int, int, int]],
    cx: float,
    cy: float,
) -> tuple[int, int, int, int] | None:
    """Spiral outward from centre until a non-overlapping bbox is found."""
    margin = FRAME_EXPORT_MARGIN
    gap = FRAME_EXPORT_GAP
    step = max(28, min(w, h) // 6)
    max_r = math.hypot(FRAME_W, FRAME_H)
    for ring in range(0, int(max_r), step):
        samples = max(40, ring // 8)
        for k in range(samples):
            theta = (k / samples) * math.pi * 2.0
            left = int(round(cx + ring * math.cos(theta) - w / 2))
            top = int(round(cy + ring * math.sin(theta) - h / 2))
            if left < margin or top < margin:
                continue
            if left + w > FRAME_W - margin or top + h > FRAME_H - margin:
                continue
            box = (left, top, left + w, top + h)
            if any(_boxes_overlap(box, other, gap) for other in placed):
                continue
            return box
    return None


def _pack_tiles(
    prepared: list[dict[str, Any]],
    area_scale: float,
) -> list[tuple[dict[str, Any], tuple[int, int, int, int], int, int]] | None:
    """Return [(row, box, w, h), ...] or None if birds cannot fit at this scale."""
    n = len(prepared)
    if not n:
        return None

    cx, cy = FRAME_W / 2, FRAME_H / 2
    # Less total ink as species count rises — keeps silhouettes separated.
    cover = min(0.42, 0.22 + 0.012 * n) * area_scale
    budget = FRAME_W * FRAME_H * cover
    scores = [math.pow(item["n"], 0.55) for item in prepared]
    score_sum = sum(scores) or 1.0
    min_side = max(72, int(min(FRAME_W, FRAME_H) * 0.045 * area_scale))

    tiles: list[tuple[dict[str, Any], int, int, Image.Image]] = []
    for item, score in zip(prepared, scores):
        cutout = item["im"]
        ar = cutout.width / max(cutout.height, 1)
        area = max(min_side * min_side, budget * (score / score_sum))
        tile_h = max(min_side, round(math.sqrt(area / max(ar, 0.35))))
        tile_w = max(min_side, round(tile_h * ar))
        tiles.append((item, tile_w, tile_h, cutout))

    tiles.sort(key=lambda t: t[1] * t[2], reverse=True)
    placed_boxes: list[tuple[int, int, int, int]] = []
    packed: list[tuple[dict[str, Any], tuple[int, int, int, int], int, int]] = []

    for item, tile_w, tile_h, cutout in tiles:
        margin = FRAME_EXPORT_MARGIN
        if not placed_boxes:
            left = int(round(cx - tile_w / 2))
            top = int(round(cy - tile_h / 2))
            left = max(margin, min(FRAME_W - margin - tile_w, left))
            top = max(margin, min(FRAME_H - margin - tile_h, top))
            box = (left, top, left + tile_w, top + tile_h)
        else:
            box = _find_open_slot(tile_w, tile_h, placed_boxes, cx, cy)
            if box is None:
                return None
        placed_boxes.append(box)
        packed.append((item, box, tile_w, tile_h))

    return packed


def export_frame_jpg(
    *,
    fetch_window: Callable[[int], list[dict[str, Any]]],
    resolve_cutout: Callable[[str, int], Path | None],
) -> bool:
    """Render the current heard-recently poster to FRAME_EXPORT_DIR/current.jpg."""
    if not FRAME_EXPORT_ENABLED:
        return False

    species = fetch_window(FRAME_EXPORT_HOURS)
    rows = []
    for row in species:
        sci = row.get("sci") or ""
        if not sci:
            continue
        cutout = resolve_cutout(sci, FRAME_EXPORT_POSE)
        if not cutout:
            continue
        n = max(1, int(row.get("n") or 1))
        rows.append({"sci": sci, "com": row.get("com") or sci, "n": n, "path": cutout})

    rows.sort(key=lambda r: r["n"], reverse=True)
    rows = rows[:FRAME_EXPORT_MAX_BIRDS]
    if not rows:
        log.info("Frame export skipped: no illustrated species in %s window", _window_label(FRAME_EXPORT_HOURS))
        return False

    max_n = max(r["n"] for r in rows)
    prepared: list[dict[str, Any]] = []
    base_side = min(FRAME_W, FRAME_H) * 0.28
    for row in rows:
        cutout = _load_cutout(row["path"], max_side=round(base_side))
        if cutout is None:
            continue
        prepared.append({**row, "im": cutout})

    if not prepared:
        log.info("Frame export skipped: no readable cutouts")
        return False

    packed = None
    for area_scale in (1.0, 0.88, 0.76, 0.65, 0.55, 0.46):
        packed = _pack_tiles(prepared, area_scale)
        if packed is not None:
            break

    if packed is None:
        # Drop smallest birds until a non-overlapping layout fits.
        trimmed = prepared[:]
        while len(trimmed) > 3 and packed is None:
            trimmed.pop()
            for area_scale in (0.88, 0.76, 0.65, 0.55):
                packed = _pack_tiles(trimmed, area_scale)
                if packed is not None:
                    break

    if packed is None:
        log.warning("Frame export failed: could not pack %d birds without overlap", len(prepared))
        return False

    canvas = Image.new("RGB", (FRAME_W, FRAME_H), FRAME_BG)
    for item, box, tile_w, tile_h in packed:
        left, top = box[0], box[1]
        fitted = item["im"].resize((tile_w, tile_h), Image.Resampling.LANCZOS)
        canvas.paste(fitted, (left, top), fitted.getchannel("A"))
    rows = [{"sci": item["sci"], "com": item["com"], "n": item["n"]} for item, *_ in packed]

    FRAME_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _output_path().with_suffix(".jpg.part")
    canvas.save(tmp, format="JPEG", quality=92, optimize=True, subsampling=0)
    data = tmp.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    tmp.replace(_output_path())

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = {
        "ready": True,
        "updated_at": now,
        "hours": FRAME_EXPORT_HOURS,
        "window": _window_label(FRAME_EXPORT_HOURS),
        "species_count": len(rows),
        "max_detections": max_n,
        "file": OUTPUT_NAME,
        "bytes": len(data),
        "sha256": digest,
        "width": FRAME_W,
        "height": FRAME_H,
        "species": [{"sci": r["sci"], "com": r["com"], "n": r["n"]} for r in rows],
    }
    _write_status(payload)
    log.info(
        "Frame export wrote %s (%d species, %d bytes, %s window)",
        _output_path(),
        len(rows),
        len(data),
        _window_label(FRAME_EXPORT_HOURS),
    )
    return True


def _run_export(
    fetch_window: Callable[[int], list[dict[str, Any]]],
    resolve_cutout: Callable[[str, int], Path | None],
) -> None:
    with _lock:
        try:
            export_frame_jpg(fetch_window=fetch_window, resolve_cutout=resolve_cutout)
        except Exception:
            log.exception("Frame export failed")


def schedule_frame_export(
    fetch_window: Callable[[int], list[dict[str, Any]]],
    resolve_cutout: Callable[[str, int], Path | None],
    delay: float = 8.0,
) -> None:
    """Debounced export after illustrations or detections change."""
    if not FRAME_EXPORT_ENABLED:
        return
    global _export_timer

    def fire() -> None:
        _run_export(fetch_window, resolve_cutout)

    with _lock:
        if _export_timer is not None:
            _export_timer.cancel()
        _export_timer = threading.Timer(delay, fire)
        _export_timer.daemon = True
        _export_timer.start()


def start_frame_export_loop(
    fetch_window: Callable[[int], list[dict[str, Any]]],
    resolve_cutout: Callable[[str, int], Path | None],
) -> None:
    if not FRAME_EXPORT_ENABLED:
        log.info("Frame export disabled (FRAME_EXPORT_ENABLED=0)")
        return

    def loop() -> None:
        time.sleep(20)
        while True:
            _run_export(fetch_window, resolve_cutout)
            time.sleep(FRAME_EXPORT_INTERVAL)

    threading.Thread(target=loop, name="frame-export", daemon=True).start()
    log.info(
        "Frame export enabled -> %s (%dx%d, every %ds, %s window)",
        FRAME_EXPORT_DIR,
        FRAME_W,
        FRAME_H,
        FRAME_EXPORT_INTERVAL,
        _window_label(FRAME_EXPORT_HOURS),
    )
