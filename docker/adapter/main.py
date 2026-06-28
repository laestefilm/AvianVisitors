"""AvianVisitors API adapter for BirdNET-Go."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from bng_client import (
    API as BNG_API,
    BIRDNET_GO_URL,
    aggregate_species,
    fetch_daily_analytics,
    fetch_detections,
    fetch_in_hours,
    fetch_lifelist,
    fetch_recent,
    health_ok,
    norm_detection,
    proxy_audio_by_id,
    proxy_species_image,
    species_for_sci,
)
from cutout_post import CUTOUT_ENABLED, preload_session, recut_all_from_raw
from illustrate import (
    GEMINI_API_KEY,
    GENERATED_DIR,
    bundled_path,
    generator_configured,
    schedule_generation,
    start_backfill_loop,
)

log = logging.getLogger("adapter")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if generator_configured():
        log.info(
            "Illustration generation enabled (gemini=%s, wangp=%s)",
            "yes" if GEMINI_API_KEY else "no",
            "yes" if os.environ.get("WANGP_ROOT", "").strip() else "no",
        )
        start_backfill_loop(fetch_lifelist)
        if CUTOUT_ENABLED:
            threading.Thread(target=preload_session, name="rembg-preload", daemon=True).start()
            if os.environ.get("RECUT_FROM_RAW", "").lower() in ("1", "true", "yes"):
                threading.Thread(target=recut_all_from_raw, name="recut-from-raw", daemon=True).start()
    else:
        log.info("Illustration generation disabled (no GEMINI_API_KEY or WANGP_ROOT)")
    yield


app = FastAPI(title="AvianVisitors BirdNET-Go Adapter", version="1.0.0", lifespan=lifespan)

BASE_PATH = os.environ.get("BASE_PATH", "/collage").rstrip("/")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "birdnet_go": BIRDNET_GO_URL,
        "birdnet_go_reachable": health_ok(),
        "base_path": BASE_PATH,
    }


@app.get("/birdnet-api.php")
def birdnet_api(
    action: str = "stats",
    hours: int = Query(24, ge=1, le=1000000),
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(10, ge=1, le=100),
    sci: str = "",
) -> JSONResponse:
    now = datetime.now(timezone.utc)
    as_of = now.isoformat()

    try:
        if action == "stats":
            lifelist = fetch_lifelist()
            today = datetime.now().date().isoformat()
            recent_day = fetch_in_hours(24)
            recent_hour = fetch_in_hours(1)
            week = fetch_in_hours(24 * 7)
            total_det = sum(int(s.get("n") or 0) for s in lifelist)
            first = min((s.get("first_seen") for s in lifelist if s.get("first_seen")), default=None)
            today_dets = [d for d in recent_day if d.get("d") == today]
            payload = {
                "totals": {"detections": total_det, "species": len(lifelist)},
                "today": {
                    "detections": len(today_dets),
                    "species": len({d.get("sci") for d in today_dets if d.get("sci")}),
                },
                "last_hour": {"detections": len(recent_hour)},
                "week": {
                    "detections": len(week),
                    "species": len({d.get("sci") for d in week if d.get("sci")}),
                },
                "started": (first or "")[:10] or None,
                "as_of": as_of,
            }
            return JSONResponse(payload)

        if action == "lifelist":
            return JSONResponse({"species": fetch_lifelist(), "as_of": as_of})

        if action == "recent":
            dets = fetch_in_hours(hours)
            species = aggregate_species(dets)
            return JSONResponse({"hours": hours, "species": species, "as_of": as_of})

        if action == "species":
            if not sci:
                raise HTTPException(status_code=400, detail="sci= required")
            summary, detections = species_for_sci(sci)
            if not summary:
                raise HTTPException(status_code=404, detail="species not found")
            return JSONResponse({"sci": sci, "summary": summary, "detections": detections})

        if action == "timeseries":
            daily, hourly = fetch_daily_analytics(days)
            return JSONResponse({"days": days, "daily": daily, "by_hour": hourly, "as_of": as_of})

        if action == "firstseen":
            rows = fetch_lifelist()
            rows.sort(key=lambda r: r.get("first_seen") or "", reverse=True)
            return JSONResponse({"species": rows[:limit], "as_of": as_of})

        if action == "illustrated":
            rows = fetch_lifelist()
            ready = [r["sci"] for r in rows if r.get("sci") and bundled_path(r["sci"], 1)]
            return JSONResponse({"ready": ready, "as_of": as_of})

        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
    except httpx.HTTPError as exc:
        log.exception("BirdNET-Go request failed")
        raise HTTPException(status_code=502, detail=f"BirdNET-Go unreachable: {exc}") from exc


def _png_cache_headers(path: Path) -> dict[str, str]:
    """Bundled library PNGs cache long; on-demand generated PNGs cache shorter."""
    try:
        generated_root = GENERATED_DIR.resolve()
        if path.resolve().is_relative_to(generated_root):
            mtime = int(path.stat().st_mtime)
            return {
                "Cache-Control": "public, max-age=300",
                "ETag": f'"{mtime}"',
            }
    except (OSError, ValueError):
        pass
    return {"Cache-Control": "public, max-age=86400"}


@app.api_route("/cutout.php", methods=["GET", "HEAD"])
def cutout(
    request: Request,
    sci: str = Query(..., min_length=3),
    pose: int = Query(1, ge=1, le=99),
    com: str = Query(""),
    generated_only: bool = Query(False),
) -> Response:
    if not sci.replace(" ", "").replace(".", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid sci")
    path = bundled_path(sci, pose)
    if path:
        headers = _png_cache_headers(path)
        if request.method == "HEAD":
            return Response(status_code=200, media_type="image/png", headers=headers)
        return FileResponse(path, media_type="image/png", headers=headers)
    if pose != 1:
        path = bundled_path(sci, 1)
        if path:
            headers = _png_cache_headers(path)
            if request.method == "HEAD":
                return Response(status_code=200, media_type="image/png", headers=headers)
            return FileResponse(path, media_type="image/png", headers=headers)
    queued = schedule_generation(sci, com, pose)
    if not generated_only:
        try:
            upstream = proxy_species_image(sci)
            if upstream.is_success and upstream.content:
                headers = {"Cache-Control": "public, max-age=300"}
                if queued:
                    headers["X-Avian-Generate"] = "queued"
                ctype = upstream.headers.get("content-type", "image/png")
                if request.method == "HEAD":
                    return Response(status_code=200, media_type=ctype, headers=headers)
                return Response(content=upstream.content, media_type=ctype, headers=headers)
        except httpx.HTTPError:
            pass
    if queued:
        if request.method == "HEAD":
            return Response(status_code=202, headers={"X-Avian-Generate": "queued"})
        raise HTTPException(status_code=202, detail="illustration generation queued")
    raise HTTPException(status_code=404, detail="no illustration")


@app.get("/recording.php")
def recording(
    sci: str = Query(""),
    file: str = Query(""),
) -> Response:
    detection_id = file.strip()
    if not detection_id and sci:
        dets = fetch_in_hours(24 * 30, limit=500)
        best = None
        for d in dets:
            if d.get("sci") != sci:
                continue
            if best is None or float(d.get("conf") or 0) > float(best.get("conf") or 0):
                best = d
        detection_id = str(best.get("id") or "") if best else ""
    if not detection_id:
        raise HTTPException(status_code=404, detail="no recording")
    try:
        upstream = proxy_audio_by_id(detection_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not upstream.is_success:
        raise HTTPException(status_code=upstream.status_code, detail="audio unavailable")
    ctype = upstream.headers.get("content-type", "audio/mpeg")
    return Response(content=upstream.content, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/wiki.php")
def wiki(sci: str = Query(...)) -> JSONResponse:
    return JSONResponse(
        {
            "sci": sci,
            "com": sci,
            "description": "",
            "source": "birdnet-go",
        }
    )


@app.get("/events/detections")
async def relay_detection_sse(request: Request) -> StreamingResponse:
    """Relay BirdNET-Go detection SSE to the browser (low CPU vs polling)."""

    async def stream() -> Any:
        url = f"{BNG_API}/detections/stream"
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes():
                        if await request.is_disconnected():
                            break
                        yield chunk
        except httpx.HTTPError as exc:
            yield f"event: error\ndata: {exc}\n\n".encode("utf-8")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
