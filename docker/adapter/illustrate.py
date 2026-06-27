"""On-demand bird illustration generation (WanGP or Gemini fallback)."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("illustrate")

ILLUSTRATIONS_DIR = Path(os.environ.get("ILLUSTRATIONS_DIR", "/app/avian/assets/illustrations"))
GENERATED_DIR = Path(os.environ.get("GENERATED_DIR", "/data/generated"))
PROMPT_TEMPLATE = Path(os.environ.get("PROMPT_TEMPLATE", "/app/avian/scripts/prompt.template.md"))
WANGP_ROOT = os.environ.get("WANGP_ROOT", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
WANGP_MODEL = os.environ.get("WANGP_MODEL", "qwen_image_20B")
WANGP_RESOLUTION = os.environ.get("WANGP_RESOLUTION", "1024x1024")

_lock = threading.Lock()
_inflight: set[str] = set()

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-image:generateContent"
)


def slugify_sci(sci: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", sci.lower())
    return slug.strip("-")


def bundled_path(sci: str, pose: int = 1) -> Path | None:
    slug = slugify_sci(sci)
    suffix = "" if pose == 1 else f"-{pose}"
    for base in (ILLUSTRATIONS_DIR, GENERATED_DIR):
        path = base / f"{slug}{suffix}.png"
        if path.is_file() and path.stat().st_size > 1024:
            return path
    return None


def _load_prompt(sci: str, com: str, pose: str) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8") if PROMPT_TEMPLATE.is_file() else (
        "Scientific illustration of {com_name} ({sci_name}), {pose}, on a flat cream background."
    )
    return template.format(sci_name=sci, com_name=com, pose=pose)


def _generate_gemini(sci: str, com: str, pose: int, dest: Path) -> bool:
    if not GEMINI_API_KEY:
        return False
    pose_label = "perched" if pose == 1 else "in flight with wings spread"
    prompt = _load_prompt(sci, com, pose_label)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    req = urllib.request.Request(
        GEMINI_URL + "?key=" + GEMINI_API_KEY,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("Gemini generation failed for %s: %s", sci, exc)
        return False
    parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if not inline:
            continue
        data = inline.get("data")
        if not data:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(__import__("base64").b64decode(data))
        return dest.stat().st_size > 1024
    return False


def _generate_wangp(sci: str, com: str, pose: int, dest: Path) -> bool:
    root = Path(WANGP_ROOT)
    if not root.is_dir():
        return False
    pose_label = "perched" if pose == 1 else "in flight with wings spread"
    prompt = _load_prompt(sci, com, pose_label)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from shared.api import init  # type: ignore
    except ImportError:
        log.warning("WanGP shared.api not importable from %s", root)
        return False
    try:
        session = init(root=root)
        settings = {
            "model_type": WANGP_MODEL,
            "prompt": prompt,
            "resolution": WANGP_RESOLUTION,
            "image_mode": 1,
        }
        job = session.submit_task(settings)
        result = job.result(timeout=600)
    except Exception as exc:
        log.warning("WanGP generation failed for %s: %s", sci, exc)
        return False
    if not result:
        return False
    out = None
    if isinstance(result, dict):
        out = result.get("output") or result.get("path") or result.get("image")
        if isinstance(out, list) and out:
            out = out[0]
    if isinstance(out, (str, Path)) and Path(out).is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(out).read_bytes())
        return dest.stat().st_size > 1024
    return False


def _generate_worker(sci: str, com: str, pose: int) -> None:
    key = f"{sci}:{pose}"
    slug = slugify_sci(sci)
    suffix = "" if pose == 1 else f"-{pose}"
    dest = GENERATED_DIR / f"{slug}{suffix}.png"
    try:
        if _generate_wangp(sci, com, pose, dest) or _generate_gemini(sci, com, pose, dest):
            log.info("Generated illustration for %s pose=%s -> %s", sci, pose, dest)
        else:
            log.info("No generator available for %s", sci)
    finally:
        with _lock:
            _inflight.discard(key)


def schedule_generation(sci: str, com: str = "", pose: int = 1) -> bool:
    """Queue background generation if missing. Returns True if queued."""
    if bundled_path(sci, pose):
        return False
    key = f"{sci}:{pose}"
    with _lock:
        if key in _inflight:
            return True
        if not WANGP_ROOT and not GEMINI_API_KEY:
            return False
        _inflight.add(key)
    thread = threading.Thread(target=_generate_worker, args=(sci, com or sci, pose), daemon=True)
    thread.start()
    return True
