# AvianVisitors collage for BirdNET-Go

Run the AvianVisitors bird collage as a **separate Docker container** alongside your existing BirdNET-Go setup. BirdNET-Go keeps its own UI; the collage lives at a dedicated path.

## Quick start (Ubuntu, same machine as BirdNET-Go)

```bash
cd docker
cp .env.example .env
# Edit .env if BirdNET-Go is not on host port 8180
docker compose up -d --build
```

Open: **http://192.168.50.108:8182/** (default — served at container root)

Or with subpath: set `BASE_PATH=/collage` in `.env` → **http://192.168.50.108:8182/collage/** (note trailing slash)

BirdNET-Go dashboard (unchanged): **http://192.168.50.108:8180/**

**Do not** open `http://192.168.50.108:8180/collage/` unless you add a reverse proxy — BirdNET-Go does not serve that path and will return 404.

## How it works

```
Browser ──► avian-collage container (:8182)
                 ├── nginx serves static collage (apt.js, illustrations)
                 └── Python adapter translates AvianVisitors API calls
                          │
                          ▼
                 BirdNET-Go (:8180)  /api/v2/detections, media, SSE
```

The adapter exposes the same JSON endpoints the Pi version uses (`birdnet-api.php`, `cutout.php`, `recording.php`). The frontend polls for stats and listens to a **relayed SSE stream** for real-time collage updates (one lightweight JSON refresh per detection, not a full 30s poll loop).

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `BIRDNET_GO_URL` | `http://host.docker.internal:8180` | Your BirdNET-Go instance |
| `AVIAN_PORT` | `8182` | Host port for the collage container |
| `BASE_PATH` | `/collage` | URL path prefix |
| `GEMINI_API_KEY` | (empty) | Generate missing illustrations via Gemini |
| `ILLUSTRATION_BACKFILL` | `1` | Scan lifelist on startup and queue missing art |
| `ILLUSTRATION_BACKFILL_INTERVAL` | `3600` | Re-scan interval in seconds (`0` = startup only) |
| `GENERATION_MAX_CONCURRENT` | `2` | Max parallel Gemini/WanGP jobs |
| `ILLUSTRATION_CUTOUT` | `1` | Auto-remove cream ground via rembg after Gemini |
| `USE_ANTI_REF` | `1` | Attach lookalike anti-refs when bundled files exist |
| `WANGP_ROOT` | (empty) | Path to Wan2GP install inside container |
| `WANGP_MODEL` | `qwen_image_20B` | WanGP model alias |

| `ILLUSTRATION_FLIGHT_POSE` | `1` | Generate flight pose (`-2.png`) after perched |
| `FRAME_EXPORT_ENABLED` | `1` | Export 3840×2160 JPEG for Samsung Frame / Home Assistant |
| `FRAME_EXPORT_HOURS` | `0` | Detection window for the poster (`0` = today since midnight) |
| `FRAME_EXPORT_INTERVAL` | `300` | Re-export interval in seconds |
| `FRAME_EXPORT_HOST_PATH` | `./frame-export` | Host folder bind-mounted to `/data/frame-export` |

### `.env` example

```env
BIRDNET_GO_URL=http://192.168.50.108:8180
AVIAN_PORT=8182
BASE_PATH=/collage
# GEMINI_API_KEY=your-key
# WANGP_ROOT=/wangp
```

## Illustrations

Bundled kachō-e PNGs ship inside the image under `/app/avian/assets/illustrations/`.

When a species has no bundled illustration:

1. Gemini generates on a **cream paper ground** (same prompt as the Pi pipeline), with reference images when available.
2. **rembg/BiRefNet cutout** runs automatically — you do not run `cutout.py` manually. This removes the cream background and produces a transparent PNG.
3. Generated PNGs persist in the `avian-generated` Docker volume.

### Reference images (same as Pi `pregen.py`)

| Ref | Source | Required? |
|-----|--------|-----------|
| **Species photo (IMAGE 1)** | Wikipedia (auto-fetched, cached in `avian-refs-cache` volume) | Auto — works offline after first fetch |
| **Style print (IMAGE 3)** | Koson/Yoshida kachō-e JPGs in `avian/assets/references/styles/` | Recommended — copy from a full AvianVisitors checkout |
| **Anti-ref (IMAGE 2)** | `_anti_bluejay.jpg`, `_anti_barnswallow.jpg` in `references/` | Optional — only for jays/swallows; set `USE_ANTI_REF=0` to disable |

Copy style references before building, or mount them:

```bash
# From a machine that has the full AvianVisitors assets:
scp -r avian/assets/references/styles/ nuc:/opt/stacks/birdcollage/AvianVisitors/avian/assets/references/
```

Or bind-mount in `docker-compose.override.yml`:

```yaml
services:
  avian-collage:
    volumes:
      - /path/to/references:/app/avian/assets/references:ro
```

Without style refs, generation still works (Wikipedia anatomy + text prompt only).

### Transparency / checkerboard artifacts

Gemini cannot render true transparency. The grey-and-white checkerboard you saw means the model tried anyway. The Pi pipeline never relies on that — it generates on **cream**, then **cutout** removes the ground. That cutout now runs inside the container after every generation.

To regenerate birds that already have bad PNGs:

```bash
docker exec avian-collage rm -f /data/generated/*.png
docker restart avian-collage
```

First cutout run downloads the BiRefNet model (~1 GB) into the container.

Generation is **not** tied to new detections. It runs when:

- Something requests `/cutout.php` for that species (collage, atlas, or stats tab), or
- The **startup backfill** scans your BirdNET-Go **lifelist** (species you have actually detected, not every bird in the world) and queues any missing species (`ILLUSTRATION_BACKFILL=1`, default on).

Backfill repeats every hour by default (`ILLUSTRATION_BACKFILL_INTERVAL=3600`). Set to `0` for startup-only.

On the collage view (`collageGeneratedOnly`), only bundled or Gemini-generated PNGs are shown — BirdNET-Go thumbnail fallbacks are used on atlas/stats only, not the collage.

Check progress: `docker logs avian-collage 2>&1 | grep -iE 'illustration|gemini|queued'`

### WanGP (Wan2GP)

[Wan2GP](https://github.com/deepbeepmeep/Wan2GP) exposes an in-process Python API (`shared.api`), not HTTP. To use it from Docker:

```yaml
# docker-compose.override.yml
services:
  avian-collage:
    environment:
      WANGP_ROOT: /wangp
      WANGP_MODEL: qwen_image_20B
    volumes:
      - /home/you/Wan2GP:/wangp:ro
```

WanGP needs a GPU and is heavy — expect the first on-demand generation to take minutes. Gemini is lighter for occasional missing species but needs an API key and billing.

**Note:** WanGP inside Docker typically requires NVIDIA runtime and mounting the full Wan2GP tree; many users run WanGP on the host and only mount it read-only. If GPU passthrough is impractical, use `GEMINI_API_KEY` instead.

## Same port as BirdNET-Go (`/collage` on :8180)

BirdNET-Go does not natively serve arbitrary static paths. To expose both UIs on port 8180, put a reverse proxy in front:

```nginx
# /etc/nginx/sites-available/birdnet
server {
    listen 8180;

    location /collage/ {
        proxy_pass http://127.0.0.1:8182/collage/;
        proxy_http_version 1.1;
        proxy_buffering off;  # required for SSE
    }

    location / {
        proxy_pass http://127.0.0.1:8180;  # BirdNET-Go's actual port — adjust if needed
    }
}
```

Alternatively, keep separate ports (8180 BirdNET-Go, 8182 collage) — simplest and recommended.

## Samsung Frame / Home Assistant export

The adapter renders a **3840×2160 JPEG** poster (cream background, heard-recently birds with illustrated cutouts) for Samsung The Frame Art Mode. Home Assistant only needs to upload the file when your TV is already in Art Mode.

### Output files

Bind-mounted from the container (default `./frame-export` next to `docker-compose.yml`):

| File | Purpose |
|------|---------|
| `current.jpg` | 4K JPEG ready for `samsungtv_smart.art_upload` |
| `current.json` | Metadata: `updated_at`, `sha256`, `species_count`, species list |

HTTP (if HA cannot mount the folder):

- JPEG: `http://<host>:8182/collage/api/frame.jpg` (or `/api/frame.jpg` without subpath)
- Status: `http://<host>:8182/collage/api/frame.json`
- Status (alt): `http://<host>:8182/collage/api/birdnet-api.php?action=frame`

Re-export runs every `FRAME_EXPORT_INTERVAL` seconds (default 5 min) and after new illustrations are saved.

### Home Assistant setup

1. Install **[ha-samsungtv-smart](https://github.com/TheFab21/ha-samsungtv-smart)** (HACS) and pair your Frame TV.
2. Mount the export folder into HA, **or** download via `wget` in a shell command:

```yaml
# configuration.yaml — optional folder sensor
sensor:
  - platform: folder
    folder: /config/www/avian-frame
    filter: "current.jpg"
```

Point HA at the host path where `FRAME_EXPORT_HOST_PATH` is mounted, or copy `current.jpg` there periodically.

3. **Automation — upload only when Art Mode is on and the poster changed:**

```yaml
automation:
  - alias: "Frame TV — sync Avian poster"
    trigger:
      - platform: time_pattern
        minutes: "/5"
    condition:
      - condition: state
        entity_id: switch.samsung_frame_art_mode   # or your Frame art switch
        state: "on"
    action:
      - service: samsungtv_smart.art_upload
        target:
          entity_id: media_player.samsung_frame
        data:
          file_path: /config/www/avian-frame/current.jpg
          file_type: jpg
```

Use a **REST sensor** on `frame.json` and compare `updated_at` or `sha256` in the condition if you only want to upload when the image actually changed:

```yaml
rest:
  - resource: http://192.168.50.108:8182/collage/api/frame.json
    sensor:
      - name: avian_frame_updated
        value_template: "{{ value_json.updated_at }}"
        json_attributes:
          - sha256
          - species_count
```

```yaml
condition:
  - condition: template
    value_template: >
      {{ states('sensor.avian_frame_updated') != states('input_text.avian_frame_last_sync') }}
```

After a successful upload, store the new `updated_at` in an `input_text` helper so unchanged posters are not re-sent.

4. **Do not push when Art Mode is off** — your `switch.samsung_*_frame_art_mode` condition handles that; the collage keeps updating `current.jpg` in the background regardless.

## Branch

This Docker stack lives on branch **`birdnet-go-collage`**, branched from `avian-visitors`.

## Health check

```bash
curl http://127.0.0.1:8182/health
curl http://127.0.0.1:8182/collage/api/birdnet-api.php?action=stats
```
