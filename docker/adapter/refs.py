"""Reference images for Gemini illustration generation (ported from pregen.py)."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

log = logging.getLogger("refs")

USER_AGENT = "AvianVisitors/1.0 (https://github.com/Twarner491/AvianVisitors)"
REF_EXTS = (".jpg", ".png")

REFS_DIR = Path(os.environ.get("REFS_DIR", "/app/avian/assets/references"))
STYLES_DIR = Path(os.environ.get("STYLES_DIR", str(REFS_DIR / "styles")))
REF_CACHE_DIR = Path(os.environ.get("REF_CACHE_DIR", "/data/references"))
USE_ANTI_REF = os.environ.get("USE_ANTI_REF", "1").lower() in ("1", "true", "yes")

JAY_GENERA = {
    "Cyanocitta", "Aphelocoma", "Cyanolyca", "Calocitta", "Cyanopica",
    "Garrulus", "Cyanocorax", "Gymnorhinus",
}
SWALLOW_GENERA = {
    "Tachycineta", "Riparia", "Progne", "Petrochelidon", "Stelgidopteryx",
}

STYLE_REFS = {
    "small_songbird_perched": "01-sparrows-on-bamboo-Koson.jpg",
    "dark_bird_perched": "02-cawing-crow-Koson.jpg",
    "vivid_perched": "03-jays-on-berry-tree-Koson.jpg",
    "vibrant_perched": "04-kingfisher-Koson.jpg",
    "owl": "05-owl-on-ginkgo-Koson.jpg",
    "large_flight": "06-goose-flying-in-moonlight-Koson.jpg",
    "small_flight": "07-swallows-in-flight-Koson.jpg",
    "wader": "08-crane-in-small-water-Koson.jpg",
    "pale_perched": "09-cockatoo-Yoshida.jpg",
    "waterfowl_perched": "10-mandarin-ducks-Yoshida.jpg",
}

GENUS_STYLE_PERCHED = {
    "Tyto": "owl", "Bubo": "owl", "Asio": "owl", "Megascops": "owl", "Athene": "owl",
    "Strix": "owl", "Glaucidium": "owl", "Aegolius": "owl",
    "Calypte": "vibrant_perched", "Archilochus": "vibrant_perched",
    "Selasphorus": "vibrant_perched", "Calothorax": "vibrant_perched",
    "Cyanocitta": "vibrant_perched", "Aphelocoma": "vibrant_perched",
    "Pica": "vibrant_perched", "Nucifraga": "vibrant_perched", "Perisoreus": "vibrant_perched",
    "Bombycilla": "vivid_perched", "Icterus": "vivid_perched", "Piranga": "vivid_perched",
    "Pheucticus": "vivid_perched", "Passerina": "vivid_perched", "Cardellina": "vivid_perched",
    "Setophaga": "vivid_perched", "Icteria": "vivid_perched",
    "Corvus": "dark_bird_perched", "Coragyps": "dark_bird_perched",
    "Cathartes": "dark_bird_perched", "Gymnogyps": "dark_bird_perched",
    "Anas": "waterfowl_perched", "Aix": "waterfowl_perched", "Mareca": "waterfowl_perched",
    "Spatula": "waterfowl_perched", "Branta": "waterfowl_perched", "Anser": "waterfowl_perched",
    "Cygnus": "waterfowl_perched", "Aythya": "waterfowl_perched", "Bucephala": "waterfowl_perched",
    "Lophodytes": "waterfowl_perched", "Mergus": "waterfowl_perched", "Oxyura": "waterfowl_perched",
    "Podiceps": "waterfowl_perched", "Podilymbus": "waterfowl_perched",
    "Aechmophorus": "waterfowl_perched", "Gavia": "waterfowl_perched",
    "Pelecanus": "waterfowl_perched", "Phalacrocorax": "waterfowl_perched", "Urile": "waterfowl_perched",
    "Ardea": "wader", "Egretta": "wader", "Bubulcus": "wader", "Butorides": "wader",
    "Nycticorax": "wader", "Plegadis": "wader", "Limosa": "wader", "Numenius": "wader",
    "Himantopus": "wader", "Recurvirostra": "wader", "Charadrius": "wader",
    "Actitis": "wader", "Calidris": "wader", "Tringa": "wader",
    "Larus": "pale_perched", "Leucophaeus": "pale_perched", "Sterna": "pale_perched",
    "Thalasseus": "pale_perched", "Hydroprogne": "pale_perched", "Rynchops": "pale_perched",
}

LARGE_FLIGHT_GENERA = {
    "Tyto", "Bubo", "Asio", "Megascops", "Athene", "Strix", "Glaucidium", "Aegolius",
    "Anas", "Aix", "Mareca", "Spatula", "Branta", "Anser", "Cygnus", "Aythya",
    "Bucephala", "Lophodytes", "Mergus", "Oxyura", "Pelecanus", "Phalacrocorax",
    "Urile", "Ardea", "Egretta", "Bubulcus", "Butorides", "Nycticorax", "Plegadis",
    "Limosa", "Numenius", "Himantopus", "Recurvirostra",
    "Buteo", "Accipiter", "Aquila", "Circus", "Falco", "Cathartes", "Coragyps",
    "Haliaeetus", "Pandion", "Elanus", "Gymnogyps", "Corvus",
}

ANTI_REFS = {
    "bluejay": {
        "common_name": "Blue Jay",
        "sci_name": "Cyanocitta cristata",
        "do_not_copy": (
            "its facial mask, its white wingbars, its black necklace, "
            "its crest pattern, or its white-tipped tail"
        ),
    },
    "barnswallow": {
        "common_name": "Barn Swallow",
        "sci_name": "Hirundo rustica",
        "do_not_copy": (
            "its deep rufous throat, its long deeply forked outer tail "
            "streamers, or its blue-black back"
        ),
    },
}

ANTI_REF_TRIGGERS = (
    (JAY_GENERA, "bluejay", "Cyanocitta cristata"),
    (SWALLOW_GENERA, "barnswallow", "Hirundo rustica"),
)


def slugify_sci(sci: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "application/octet-stream"


def fetch_wikipedia_thumb(sci: str, com: str) -> tuple[bytes, str] | None:
    titles = [sci.replace(" ", "_"), com.replace(" ", "_"), com.split()[0]]
    for title in titles:
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                meta = json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        for key in ("originalimage", "thumbnail"):
            src = (meta.get(key) or {}).get("source")
            if not src or not src.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            try:
                req2 = urllib.request.Request(src, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req2, timeout=30) as resp2:
                    data = resp2.read()
            except (urllib.error.HTTPError, urllib.error.URLError):
                continue
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return data, ".png"
            if data.startswith(b"\xff\xd8\xff"):
                return data, ".jpg"
    return None


def ensure_reference(slug: str, sci: str, com: str) -> Path | None:
    """Return a species anatomy reference, from cache or Wikipedia."""
    for base in (REF_CACHE_DIR, REFS_DIR):
        for ext in REF_EXTS:
            cached = base / f"{slug}{ext}"
            if cached.is_file() and cached.stat().st_size > 1024:
                return cached
    fetched = fetch_wikipedia_thumb(sci, com)
    if not fetched:
        return None
    data, ext = fetched
    REF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = REF_CACHE_DIR / f"{slug}{ext}"
    path.write_bytes(data)
    log.info("Cached Wikipedia reference for %s -> %s", sci, path.name)
    return path


def select_style_ref(sci: str, pose: int) -> str:
    genus = sci.split()[0]
    if sci == "Aeronautes saxatalis":
        return STYLE_REFS["vibrant_perched"]
    if pose == 2:
        key = "large_flight" if genus in LARGE_FLIGHT_GENERA else "small_flight"
        return STYLE_REFS[key]
    return STYLE_REFS[GENUS_STYLE_PERCHED.get(genus, "small_songbird_perched")]


def style_ref_path(sci: str, pose: int) -> Path | None:
    for base in (STYLES_DIR, REFS_DIR / "styles"):
        path = base / select_style_ref(sci, pose)
        if path.is_file() and path.stat().st_size > 1024:
            return path
    return None


def select_anti_ref_key(sci: str) -> str | None:
    genus = sci.split()[0]
    for genera, key, exclude in ANTI_REF_TRIGGERS:
        if genus in genera and sci != exclude:
            return key
    return None


def anti_ref_path(key: str) -> Path | None:
    for base in (REFS_DIR, REF_CACHE_DIR):
        path = base / f"_anti_{key}.jpg"
        if path.is_file() and path.stat().st_size > 1024:
            return path
    return None


def _anti_ref_line(anti_ref_key: str | None) -> str:
    info = ANTI_REFS.get(anti_ref_key or "")
    if not info:
        return ""
    return (
        f"- IMAGE 2 (negative, when attached) is a {info['common_name']} "
        f"({info['sci_name']}). It is NOT what you are drawing. Do NOT "
        f"copy {info['do_not_copy']}. If your output looks more like "
        f"IMAGE 2 than IMAGE 1, the output is wrong."
    )


def _encode_ref(path: Path, *, downscale: bool = False) -> tuple[str, str]:
    if downscale:
        try:
            from PIL import Image

            img = Image.open(path).convert("RGB")
            w, h = img.size
            if max(w, h) > 384:
                scale = 384 / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return "image/png", base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
    return _mime_for(path), base64.b64encode(path.read_bytes()).decode()


@dataclass
class RefSet:
    positive: Path | None
    anti: Path | None
    anti_key: str | None
    style: Path | None


def gather_refs(sci: str, com: str, pose: int) -> RefSet:
    slug = slugify_sci(sci)
    positive = ensure_reference(slug, sci, com)
    anti_key = select_anti_ref_key(sci) if USE_ANTI_REF else None
    anti = anti_ref_path(anti_key) if anti_key else None
    if anti_key and not anti:
        anti_key = None
    style = style_ref_path(sci, pose)
    return RefSet(positive=positive, anti=anti, anti_key=anti_key, style=style)


def build_gemini_parts(prompt_body: str, sci: str, com: str, pose: int) -> list[dict]:
    """Build Gemini content parts: prompt text + optional reference images."""
    refs = gather_refs(sci, com, pose)
    body = prompt_body.replace("{anti_ref_line}", _anti_ref_line(refs.anti_key))
    parts: list[dict] = [{"text": body}]

    if refs.positive:
        mime, data = _encode_ref(refs.positive, downscale=True)
        parts.append({"text": "IMAGE 1 (positive, target species):"})
        parts.append({"inline_data": {"mime_type": mime, "data": data}})
    if refs.anti and refs.anti_key:
        anti_name = ANTI_REFS.get(refs.anti_key, {}).get("common_name", "lookalike species")
        mime, data = _encode_ref(refs.anti)
        parts.append({"text": f"IMAGE 2 (negative, {anti_name}, do NOT copy):"})
        parts.append({"inline_data": {"mime_type": mime, "data": data}})
    if refs.style:
        mime, data = _encode_ref(refs.style)
        parts.append({"text": (
            "IMAGE 3 (positive STYLE reference - Edo-period kachō-e woodblock "
            "print). The species in IMAGE 3 is irrelevant; only its painting "
            "technique is borrowed (flat washes, confident outlines, tonal "
            "mineral-pigment ground). DO NOT copy any branches, leaves, water, "
            "moon, or scenery from IMAGE 3."
        )})
        parts.append({"inline_data": {"mime_type": mime, "data": data}})

    tags = []
    if refs.positive:
        tags.append("wiki")
    if refs.anti:
        tags.append("anti")
    if refs.style:
        tags.append("style")
    log.info(
        "Refs for %s: %s",
        sci,
        "+".join(tags) if tags else "text-only (add avian/assets/references/styles/ for style refs)",
    )
    return parts
