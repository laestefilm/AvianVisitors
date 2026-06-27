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
| `WANGP_ROOT` | (empty) | Path to Wan2GP install inside container |
| `WANGP_MODEL` | `qwen_image_20B` | WanGP model alias |

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

1. The adapter returns BirdNET-Go's species thumbnail immediately (if available).
2. If `WANGP_ROOT` or `GEMINI_API_KEY` is set, it **queues background generation** using the same kachō-e prompt template as the Pi pipeline.
3. Generated PNGs persist in the `avian-generated` Docker volume and are served on subsequent requests.

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

## Branch

This Docker stack lives on branch **`birdnet-go-collage`**, branched from `avian-visitors`.

## Health check

```bash
curl http://127.0.0.1:8182/health
curl http://127.0.0.1:8182/collage/api/birdnet-api.php?action=stats
```
