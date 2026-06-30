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


def _spiral_xy(index: int, total: int, cx: float, cy: float, radius: float) -> tuple[float, float]:
    if total <= 1:
        return cx, cy
    t = index / max(total - 1, 1)
    angle = t * math.pi * 2.0 * 2.75
    r = radius * math.sqrt(t)
    return cx + r * math.cos(angle), cy + r * math.sin(angle)


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
    canvas = Image.new("RGB", (FRAME_W, FRAME_H), FRAME_BG)
    cx, cy = FRAME_W / 2, FRAME_H / 2
    base = min(FRAME_W, FRAME_H) * 0.34
    budget = FRAME_W * FRAME_H * 0.52
    scores = [math.pow(r["n"], 0.55) for r in rows]
    score_sum = sum(scores) or 1.0

    placed: list[tuple[int, int, int, int]] = []
    for idx, (row, score) in enumerate(zip(rows, scores)):
        area = budget * (score / score_sum)
        cutout = _load_cutout(row["path"], max_side=round(base * 2.2))
        if cutout is None:
            continue
        ar = cutout.width / max(cutout.height, 1)
        tile_h = max(80, round(math.sqrt(area / max(ar, 0.4))))
        tile_w = max(60, round(tile_h * ar))
        x, y = _spiral_xy(idx, len(rows), cx, cy, min(FRAME_W, FRAME_H) * 0.36)
        left = int(round(x - tile_w / 2))
        top = int(round(y - tile_h / 2))
        left = max(20, min(FRAME_W - tile_w - 20, left))
        top = max(20, min(FRAME_H - tile_h - 20, top))
        box = (left, top, left + tile_w, top + tile_h)
        overlap = any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in placed)
        if overlap:
            left = int(round(cx - tile_w / 2 + (idx % 5 - 2) * tile_w * 0.12))
            top = int(round(cy - tile_h / 2 + (idx // 5 - 2) * tile_h * 0.12))
            left = max(20, min(FRAME_W - tile_w - 20, left))
            top = max(20, min(FRAME_H - tile_h - 20, top))
            box = (left, top, left + tile_w, top + tile_h)
        fitted = cutout.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
        canvas.paste(fitted, (left, top), fitted.getchannel("A"))
        placed.append(box)

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
        "Frame export enabled -> %s (%dx%d, every %ds, %sh window)",
        FRAME_EXPORT_DIR,
        FRAME_W,
        FRAME_H,
        FRAME_EXPORT_INTERVAL,
        _window_label(FRAME_EXPORT_HOURS),
    )
