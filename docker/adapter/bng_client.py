"""HTTP client for BirdNET-Go API v2."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger("bng_client")

BIRDNET_GO_URL = os.environ.get("BIRDNET_GO_URL", "http://host.docker.internal:8180").rstrip("/")
API = f"{BIRDNET_GO_URL}/api/v2"
TIMEOUT = float(os.environ.get("BNG_HTTP_TIMEOUT", "30"))
MAX_LIMIT = int(os.environ.get("BNG_MAX_LIMIT", "500"))


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True)


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "detections", "items", "results"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        if "species" in payload:
            return payload
    return payload


def _as_list(payload: Any) -> list[dict[str, Any]]:
    payload = _unwrap(payload)
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _today_local() -> date:
    return datetime.now().date()


def _parse_ts(det: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "dateTime", "datetime"):
        raw = det.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        text = str(raw).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            continue
    d = det.get("date")
    if d:
        t = det.get("time") or "00:00:00"
        try:
            return datetime.fromisoformat(f"{str(d)[:10]}T{t}")
        except ValueError:
            try:
                return datetime.fromisoformat(str(d)[:10] + "T00:00:00")
            except ValueError:
                pass
    return None


def norm_detection(det: dict[str, Any]) -> dict[str, Any]:
    ts = _parse_ts(det)
    d = ts.date().isoformat() if ts else (str(det.get("date") or "")[:10] or None)
    t = ts.strftime("%H:%M:%S") if ts else (str(det.get("time") or "")[:8] or None)
    return {
        "id": det.get("id"),
        "sci": det.get("scientificName") or det.get("scientific_name") or "",
        "com": det.get("commonName") or det.get("common_name") or "",
        "conf": float(det.get("confidence") or 0),
        "clip": det.get("clipName") or det.get("clip_name"),
        "at": ts.isoformat() if ts else None,
        "d": d,
        "t": t,
        "ts": ts,
    }


def fetch_recent(limit: int | None = None) -> list[dict[str, Any]]:
    lim = min(limit or MAX_LIMIT, MAX_LIMIT)
    with _client() as c:
        r = c.get(f"{API}/detections/recent", params={"limit": lim})
        r.raise_for_status()
        return [norm_detection(d) for d in _as_list(r.json())]


def fetch_detections(
    *,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    limit: int | None = None,
    species: str = "",
) -> list[dict[str, Any]]:
    """BirdNET-Go requires start_date AND end_date together when either is set."""
    lim = min(limit or MAX_LIMIT, MAX_LIMIT)
    params: dict[str, Any] = {"limit": lim}
    if species:
        params["species"] = species

    start_d = start.date() if isinstance(start, datetime) else start
    end_d = end.date() if isinstance(end, datetime) else end
    if start_d or end_d:
        if not end_d:
            end_d = _today_local()
        if not start_d:
            start_d = end_d - timedelta(days=30)
        params["start_date"] = start_d.isoformat()
        params["end_date"] = end_d.isoformat()

    with _client() as c:
        r = c.get(f"{API}/detections", params=params)
        r.raise_for_status()
        return [norm_detection(d) for d in _as_list(r.json())]


def fetch_in_hours(hours: int, limit: int | None = None) -> list[dict[str, Any]]:
    lim = min(limit or MAX_LIMIT, MAX_LIMIT)
    if hours >= 1000000:
        return fetch_recent(lim)

    since = datetime.now().replace(tzinfo=None) - timedelta(hours=hours)
    dets = fetch_recent(lim)
    filtered = []
    for d in dets:
        ts = d.get("ts")
        if ts is None:
            filtered.append(d)
            continue
        cmp_ts = ts.replace(tzinfo=None) if ts.tzinfo else ts
        if cmp_ts >= since:
            filtered.append(d)
    if filtered:
        return filtered

    end = datetime.now()
    return fetch_detections(start=since.date(), end=end.date(), limit=lim)


def aggregate_species(dets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_sci: dict[str, dict[str, Any]] = {}
    for d in dets:
        sci = d.get("sci") or ""
        if not sci:
            continue
        row = by_sci.get(sci)
        if not row:
            by_sci[sci] = {
                "sci": sci,
                "com": d.get("com") or sci,
                "n": 0,
                "best_conf": 0.0,
                "last_seen": None,
                "last_ts": None,
                "top_file": None,
                "top_at": None,
            }
            row = by_sci[sci]
        row["n"] += 1
        conf = float(d.get("conf") or 0)
        if conf >= row["best_conf"]:
            row["best_conf"] = conf
            row["top_file"] = d.get("clip") or str(d.get("id") or "")
            if d.get("d") and d.get("t"):
                row["top_at"] = f"{d['d']} {d['t']}"
        ts = d.get("ts")
        if ts and (row["last_ts"] is None or ts > row["last_ts"]):
            row["last_ts"] = ts
            row["last_seen"] = f"{d.get('d') or ts.date().isoformat()} {d.get('t') or ts.strftime('%H:%M:%S')}"
    out = list(by_sci.values())
    for row in out:
        row.pop("last_ts", None)
    out.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    return out


def species_for_sci(sci: str, limit: int | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    lim = min(limit or MAX_LIMIT, MAX_LIMIT)
    end = _today_local()
    start = end - timedelta(days=365)
    try:
        dets = fetch_detections(start=start, end=end, limit=lim, species=sci)
    except httpx.HTTPError:
        dets = [d for d in fetch_recent(lim) if d.get("sci") == sci]
    if not dets:
        return None, []
    dets.sort(key=lambda d: (d.get("d") or "", d.get("t") or ""), reverse=True)
    summary = {
        "com": dets[0].get("com") or sci,
        "total": len(dets),
        "first_seen": min((f"{d['d']} {d['t']}" for d in dets if d.get("d")), default=None),
        "last_seen": max((f"{d['d']} {d['t']}" for d in dets if d.get("d")), default=None),
        "best_conf": max(float(d.get("conf") or 0) for d in dets),
    }
    detections = [
        {"d": d.get("d"), "t": d.get("t"), "file": str(d.get("id") or d.get("clip") or ""), "conf": d.get("conf")}
        for d in dets[:500]
    ]
    return summary, detections


def fetch_daily_analytics(days: int = 30) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    end = _today_local()
    start = end - timedelta(days=days - 1)
    daily: list[dict[str, Any]] = []
    by_hour = [0] * 24
    with _client() as c:
        try:
            r = c.get(
                f"{API}/analytics/time/daily",
                params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )
            if r.is_success:
                body = r.json()
                rows = _as_list(body) if not isinstance(body, dict) else body.get("daily", [])
                for row in rows:
                    daily.append(
                        {
                            "date": row.get("date") or row.get("day"),
                            "detections": int(row.get("detections") or row.get("count") or 0),
                            "species": int(row.get("species") or row.get("unique_species") or 0),
                        }
                    )
        except httpx.HTTPError as exc:
            log.debug("analytics/time/daily failed: %s", exc)
        try:
            r = c.get(
                f"{API}/analytics/time/distribution/hourly",
                params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )
            if r.is_success:
                body = r.json()
                rows = body if isinstance(body, list) else body.get("hours") or body.get("by_hour") or []
                for row in rows:
                    h = int(row.get("hour") if "hour" in row else row.get("h", 0))
                    if 0 <= h < 24:
                        by_hour[h] = int(row.get("detections") or row.get("count") or 0)
        except httpx.HTTPError as exc:
            log.debug("analytics hourly failed: %s", exc)

    if not daily:
        try:
            dets = fetch_detections(start=start, end=end, limit=MAX_LIMIT)
            buckets: dict[str, dict[str, set[str]]] = {}
            for d in dets:
                day = d.get("d")
                if not day:
                    continue
                buckets.setdefault(day, {"n": set(), "species": set()})
                buckets[day]["n"].add(str(d.get("id") or id(d)))
                buckets[day]["species"].add(d.get("sci") or "")
                if d.get("ts"):
                    by_hour[d["ts"].hour] += 1
            daily = [
                {"date": day, "detections": len(v["n"]), "species": len(v["species"])}
                for day, v in sorted(buckets.items())
            ]
        except httpx.HTTPError:
            pass

    hourly = [{"hour": h, "detections": by_hour[h]} for h in range(24)]
    return daily, hourly


def fetch_lifelist() -> list[dict[str, Any]]:
    end = _today_local()
    start = end - timedelta(days=3650)
    with _client() as c:
        try:
            r = c.get(
                f"{API}/analytics/species/summary",
                params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )
            if r.is_success:
                body = r.json()
                rows = _as_list(body) if not isinstance(body, dict) else body.get("species", [])
                out = []
                for row in rows:
                    sci = row.get("scientificName") or row.get("scientific_name") or row.get("sci")
                    if not sci:
                        continue
                    out.append(
                        {
                            "sci": sci,
                            "com": row.get("commonName") or row.get("common_name") or row.get("com") or sci,
                            "first_seen": row.get("firstSeen") or row.get("first_seen"),
                            "last_seen": row.get("lastSeen") or row.get("last_seen"),
                            "n": int(row.get("count") or row.get("detections") or row.get("n") or 0),
                            "best_conf": float(row.get("bestConfidence") or row.get("best_conf") or 0),
                        }
                    )
                if out:
                    return out
        except httpx.HTTPError as exc:
            log.debug("species/summary failed: %s", exc)

    dets = fetch_recent(MAX_LIMIT)
    species = aggregate_species(dets)
    lifelist = []
    for s in species:
        sci_dets = [d for d in dets if d.get("sci") == s["sci"]]
        times = sorted(f"{d['d']} {d['t']}" for d in sci_dets if d.get("d"))
        lifelist.append(
            {
                "sci": s["sci"],
                "com": s["com"],
                "first_seen": times[0] if times else s.get("last_seen"),
                "last_seen": s.get("last_seen"),
                "n": s["n"],
                "best_conf": s["best_conf"],
            }
        )
    lifelist.sort(key=lambda r: r.get("first_seen") or "")
    return lifelist


def proxy_audio_by_id(detection_id: str) -> httpx.Response:
    with _client() as c:
        return c.get(f"{API}/media/audio", params={"id": detection_id})


def proxy_species_image(sci: str) -> httpx.Response:
    from urllib.parse import quote

    with _client() as c:
        r = c.get(f"{API}/media/species-image", params={"name": sci})
        if r.is_success and r.content:
            return r
        return c.get(f"{API}/media/image/{quote(sci, safe='')}")


def health_ok() -> bool:
    try:
        with _client() as c:
            r = c.get(f"{API}/health")
            return r.is_success
    except httpx.HTTPError:
        return False
