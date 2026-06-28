#!/usr/bin/env python3
"""Download style + anti-reference images from Wikimedia Commons."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

USER_AGENT = "AvianVisitors/1.0 (reference fetch; local setup)"
API = "https://commons.wikimedia.org/w/api.php"
ROOT = Path(__file__).resolve().parents[1] / "avian" / "assets" / "references"
STYLES = ROOT / "styles"

# Target filename -> Wikimedia Commons file title (without File: prefix)
STYLE_FILES = {
    "01-sparrows-on-bamboo-Koson.jpg": "Koson - tree-sparrow-and-bamboo.jpg",
    "02-cawing-crow-Koson.jpg": "Cawing crow by Ohara Koson.jpg",
    "03-jays-on-berry-tree-Koson.jpg": "Koson - jays-on-berry-tree.jpg",
    "04-kingfisher-Koson.jpg": "Koson - kingfisher.jpg",
    "05-owl-on-ginkgo-Koson.jpg": "Koson - owl-on-ginkgo-branch-scops-owl-under-crescent-moon.jpg",
    "06-goose-flying-in-moonlight-Koson.jpg": "Brooklyn Museum - Goose Flying in Moonlight - Ohara Koson (Shoson).jpg",
    "07-swallows-in-flight-Koson.jpg": "Koson - swallows-in-flight.jpg",
    "08-crane-in-small-water-Koson.jpg": "Koson - crane-in-small-water.jpg",
    "09-cockatoo-Yoshida.jpg": "Sulphur-crested Cockatoo by Yoshida Hiroshi, 1926, woodblock print, Honolulu Museum of Art.jpg",
    "10-mandarin-ducks-Yoshida.jpg": "Yoshida Village by Yoshida Hiroshi, 1926, woodblock print, Honolulu Museum of Art.jpg",
}

# Alternate Commons titles if the primary name 404s.
STYLE_ALT = {
    "10-mandarin-ducks-Yoshida.jpg": [
        "Spring Rain by Yoshida Hiroshi, 1935. woodblock print, Honolulu Museum of Art.jpg",
        "Musashino by Yoshida Hiroshi, 1928, woodblock print, Honolulu Museum of Art.jpg",
    ],
}

ANTI_FILES = {
    "_anti_bluejay.jpg": "Cyanocitta cristata - Blue Jay.jpg",
    "_anti_barnswallow.jpg": "Hirundo rustica - Barn Swallow.jpg",
}


def api(params: dict) -> dict:
    params = {**params, "format": "json"}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("api failed")


def commons_url(title: str) -> str | None:
    data = api(
        {
            "action": "query",
            "titles": f"File:{title}",
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": 1200,
        }
    )
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            return None
        info = (page.get("imageinfo") or [{}])[0]
        return info.get("thumburl") or info.get("url")
    return None


def search_commons(query: str) -> str | None:
    data = api(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": 6,
            "srlimit": 5,
        }
    )
    for hit in data.get("query", {}).get("search", []):
        title = hit.get("title", "")
        if title.startswith("File:"):
            return title[5:]
    return None


def thumb_url(filename: str, width: int = 800) -> str:
    """Build a Wikimedia Commons thumbnail URL without extra API calls."""
    h = hashlib.md5(filename.encode("utf-8")).hexdigest()
    enc = filename.replace(" ", "_")
    q = urllib.parse.quote(enc)
    return f"https://upload.wikimedia.org/wikipedia/commons/thumb/{h[0]}/{h[:2]}/{q}/{width}px-{q}"


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 7:
                time.sleep(15 * (attempt + 1))
                continue
            raise
    raise RuntimeError("download failed")


def save_jpeg(dest: Path, raw: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if raw.startswith(b"\xff\xd8\xff"):
        dest.write_bytes(raw)
        return
    try:
        from PIL import Image

        im = Image.open(BytesIO(raw)).convert("RGB")
        im.save(dest, format="JPEG", quality=92, optimize=True)
    except Exception:
        dest.write_bytes(raw)


def fetch_wikipedia_photo(sci: str, com: str) -> tuple[bytes, str] | None:
    titles = [sci.replace(" ", "_"), com.replace(" ", "_"), com.split()[0]]
    for title in titles:
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                meta = json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        for key in ("thumbnail", "originalimage"):
            src = (meta.get(key) or {}).get("source")
            if not src:
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


def fetch_one(dest_name: str, commons_title: str, dest_dir: Path) -> bool:
    dest = dest_dir / dest_name
    if dest.is_file() and dest.stat().st_size > 2048:
        print(f"  [skip] {dest_name} (already exists)")
        return True
    titles = [commons_title, *STYLE_ALT.get(dest_name, [])]
    for title in titles:
        urls: list[str] = []
        api_url = commons_url(title)
        if api_url:
            urls.append(api_url)
        urls.append(thumb_url(title))
        print(f"  [get]  {dest_name} <- {title}")
        for url in urls:
            try:
                raw = download(url)
            except Exception as exc:
                print(f"  [fail] {dest_name}: {exc}")
                time.sleep(5)
                continue
            if len(raw) < 2048:
                continue
            save_jpeg(dest, raw)
            print(f"  [ok]   {dest_name} ({dest.stat().st_size // 1024} KB)")
            time.sleep(8)
            return True
    return False


def fetch_anti(dest_name: str, sci: str, com: str) -> bool:
    dest = ROOT / dest_name
    if dest.is_file() and dest.stat().st_size > 2048:
        print(f"  [skip] {dest_name} (already exists)")
        return True
    fetched = fetch_wikipedia_photo(sci, com)
    if not fetched:
        title = ANTI_FILES.get(dest_name, "")
        return fetch_one(dest_name, title, ROOT) if title else False
    raw, ext = fetched
    save_jpeg(dest, raw)
    print(f"  [ok]   {dest_name} from Wikipedia ({dest.stat().st_size // 1024} KB)")
    return True


def main() -> int:
    STYLES.mkdir(parents=True, exist_ok=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    ok = 0

    print("Style references ->", STYLES)
    for dest_name, title in STYLE_FILES.items():
        if fetch_one(dest_name, title, STYLES):
            ok += 1
        else:
            # Fallback search for mandarin duck etc.
            q = dest_name.replace("-", " ").replace(".jpg", "")
            alt = search_commons(q)
            time.sleep(1.5)
            if alt and fetch_one(dest_name, alt, STYLES):
                ok += 1

    print("\nAnti-references ->", ROOT)
    fetch_anti("_anti_bluejay.jpg", "Cyanocitta cristata", "Blue Jay")
    time.sleep(5)
    fetch_anti("_anti_barnswallow.jpg", "Hirundo rustica", "Barn Swallow")

    print(f"\nDone: {ok}/{len(STYLE_FILES)} style refs in {STYLES}")
    return 0 if ok >= 8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
