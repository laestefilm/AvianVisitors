"""On-demand bird illustration generation (WanGP or Gemini fallback)."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cutout_post import cutout_png, save_raw_png
from refs import build_gemini_parts

log = logging.getLogger("illustrate")

ILLUSTRATIONS_DIR = Path(os.environ.get("ILLUSTRATIONS_DIR", "/app/avian/assets/illustrations"))
GENERATED_DIR = Path(os.environ.get("GENERATED_DIR", "/data/generated"))
PROMPT_TEMPLATE = Path(
    os.environ.get("PROMPT_TEMPLATE", "/app/avian/scripts/prompt.template.md")
)
WANGP_ROOT = os.environ.get("WANGP_ROOT", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
WANGP_MODEL = os.environ.get("WANGP_MODEL", "qwen_image_20B")
WANGP_RESOLUTION = os.environ.get("WANGP_RESOLUTION", "1024x1024")
GENERATION_MAX_CONCURRENT = max(1, int(os.environ.get("GENERATION_MAX_CONCURRENT", "2")))
ILLUSTRATION_BACKFILL = os.environ.get("ILLUSTRATION_BACKFILL", "1").lower() in ("1", "true", "yes")
ILLUSTRATION_BACKFILL_INTERVAL = max(0, int(os.environ.get("ILLUSTRATION_BACKFILL_INTERVAL", "3600")))

_lock = threading.Lock()
_inflight: set[str] = set()
_gen_slots = threading.Semaphore(GENERATION_MAX_CONCURRENT)

_PROMPT_SECTION = re.compile(r"##\s*Prompt\s*\n(.+?)(?=\n##\s|\Z)", re.DOTALL)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-image:generateContent"
)

POSES = {1: "perched", 2: "in flight with wings spread"}

_FALLBACK_PROMPT = (
    "Generate a {pose} {com_name} ({sci_name}) as an Edo-period Japanese kachō-e "
    "woodblock print. The bird sits on a CONSISTENT WARM CREAM tonal background that "
    "fills the entire frame — like aged mulberry paper. NO branch, NO scenery. "
    "Flat watercolor washes, confident ink outlines. {anti_ref_line}"
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


def _load_prompt_template() -> str:
    if PROMPT_TEMPLATE.is_file():
        text = PROMPT_TEMPLATE.read_text(encoding="utf-8")
        match = _PROMPT_SECTION.search(text)
        return (match.group(1) if match else text).strip()
    return _FALLBACK_PROMPT


def _load_prompt(sci: str, com: str, pose: int) -> str:
    """Prompt body before reference substitution (anti_ref_line filled in build_gemini_parts)."""
    template = _load_prompt_template()
    pose_label = POSES.get(pose, POSES[1])
    return (
        template.replace("{sci_name}", sci)
        .replace("{com_name}", com)
        .replace("{pose}", pose_label)
    )


def generator_configured() -> bool:
    return bool(WANGP_ROOT or GEMINI_API_KEY)


def _save_png(dest: Path, raw: bytes) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    save_raw_png(dest.name, raw)
    cut = cutout_png(raw)
    if not cut or len(cut) < 1024:
        return False
    dest.write_bytes(cut)
    return dest.stat().st_size > 1024


def _generate_gemini(sci: str, com: str, pose: int, dest: Path) -> bool:
    if not GEMINI_API_KEY:
        return False
    try:
        prompt = _load_prompt(sci, com, pose)
        parts = build_gemini_parts(prompt, sci, com, pose)
    except Exception as exc:
        log.warning("Prompt/refs build failed for %s: %s", sci, exc)
        return False
    log.info("Gemini: generating illustration for %s (%s)", sci, com)
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    req = urllib.request.Request(
        GEMINI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        log.warning("Gemini HTTP %s for %s: %s", exc.code, sci, detail)
        return False
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("Gemini request failed for %s: %s", sci, exc)
        return False
    if body.get("error"):
        log.warning("Gemini API error for %s: %s", sci, body["error"])
        return False
    for part in body.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if not inline or not inline.get("data"):
            continue
        raw = base64.b64decode(inline["data"])
        if _save_png(dest, raw):
            log.info("Gemini + cutout saved for %s -> %s", sci, dest)
            return True
    log.warning("Gemini: no image in response for %s", sci)
    return False


def _generate_wangp(sci: str, com: str, pose: int, dest: Path) -> bool:
    root = Path(WANGP_ROOT)
    if not root.is_dir():
        return False
    try:
        prompt = _load_prompt(sci, com, pose).replace("{anti_ref_line}", "")
    except Exception as exc:
        log.warning("Prompt build failed for %s: %s", sci, exc)
        return False
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
        raw = Path(out).read_bytes()
        return _save_png(dest, raw)
    return False


def _generate_worker(sci: str, com: str, pose: int) -> None:
    key = f"{sci}:{pose}"
    slug = slugify_sci(sci)
    suffix = "" if pose == 1 else f"-{pose}"
    dest = GENERATED_DIR / f"{slug}{suffix}.png"
    try:
        with _gen_slots:
            ok = _generate_wangp(sci, com, pose, dest) or _generate_gemini(sci, com, pose, dest)
        if ok:
            log.info("Generated illustration for %s pose=%s -> %s", sci, pose, dest)
        elif not generator_configured():
            log.info("Illustration skipped for %s (no GEMINI_API_KEY or WANGP_ROOT)", sci)
        else:
            log.warning("Illustration generation failed for %s pose=%s", sci, pose)
    except Exception:
        log.exception("Illustration worker crashed for %s pose=%s", sci, pose)
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
        if not generator_configured():
            return False
        _inflight.add(key)
    log.info("Queued illustration for %s (%s) pose=%s", sci, com or sci, pose)
    thread = threading.Thread(target=_generate_worker, args=(sci, com or sci, pose), daemon=True)
    thread.start()
    return True


def backfill_missing_illustrations(fetch_lifelist: Callable[[], list[dict[str, Any]]]) -> int:
    """Scan lifelist and queue generation for species without bundled/generated art."""
    if not generator_configured():
        log.info("Illustration backfill skipped (set GEMINI_API_KEY or WANGP_ROOT to enable)")
        return 0
    try:
        lifelist = fetch_lifelist()
    except Exception as exc:
        log.warning("Illustration backfill: lifelist fetch failed: %s", exc)
        return 0
    missing = [row for row in lifelist if row.get("sci") and not bundled_path(row["sci"], 1)]
    if not missing:
        log.info("Illustration backfill: all %d lifelist species have illustrations", len(lifelist))
        return 0
    log.info(
        "Illustration backfill: queuing %d of %d species missing illustrations",
        len(missing),
        len(lifelist),
    )
    queued = 0
    for row in missing:
        if schedule_generation(row["sci"], row.get("com") or "", 1):
            queued += 1
    return queued


def start_backfill_loop(fetch_lifelist: Callable[[], list[dict[str, Any]]]) -> None:
    """Run backfill on startup and optionally on an interval (seconds)."""

    def loop() -> None:
        time.sleep(8)
        while True:
            backfill_missing_illustrations(fetch_lifelist)
            if ILLUSTRATION_BACKFILL_INTERVAL <= 0:
                break
            time.sleep(ILLUSTRATION_BACKFILL_INTERVAL)

    if ILLUSTRATION_BACKFILL:
        threading.Thread(target=loop, name="illustration-backfill", daemon=True).start()
