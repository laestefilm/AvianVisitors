"""Remove cream ground from generated illustrations (Pi pipeline cutout.py step)."""

from __future__ import annotations

import logging
import os
import threading
from io import BytesIO
from pathlib import Path

log = logging.getLogger("cutout")

CUTOUT_ENABLED = os.environ.get("ILLUSTRATION_CUTOUT", "1").lower() in ("1", "true", "yes")
CUTOUT_MODEL = os.environ.get("CUTOUT_MODEL", "birefnet-general")
CUTOUT_MARGIN = float(os.environ.get("CUTOUT_MARGIN", "0.02"))

_session = None
_session_lock = threading.Lock()


def _get_session():
    global _session
    with _session_lock:
        if _session is None:
            from rembg import new_session

            log.info("Loading rembg model %s (first run may download ~1 GB)", CUTOUT_MODEL)
            _session = new_session(CUTOUT_MODEL)
        return _session


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
        im = Image.open(BytesIO(raw))
        session = _get_session()
        cut = remove(im.convert("RGB"), session=session)
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
