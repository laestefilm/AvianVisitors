#!/bin/sh
set -eu

BASE_PATH="${BASE_PATH:-}"
BASE_PATH="$(echo "$BASE_PATH" | sed 's#/*$##')"
export BASE_PATH
export COLLAGE_ONLY="${COLLAGE_ONLY:-true}"
export FULL_REFRESH_MS="${FULL_REFRESH_MS:-300000}"

python3 - <<'PY'
import os
import re
from pathlib import Path

base = os.environ.get("BASE_PATH", "").strip().rstrip("/")
adapter = "http://127.0.0.1:8090"
collage_only = os.environ.get("COLLAGE_ONLY", "true").lower() in ("1", "true", "yes")
full_refresh = os.environ.get("FULL_REFRESH_MS", "300000")

if base:
    api_base = f"{base}/api"
    sse_url = f"{api_base}/events/detections"
    html_base = f"{base}/"
    nginx = f"""server {{
    listen 80;
    server_name _;

    location = /health {{
        proxy_pass {adapter}/health;
    }}

    location = {base} {{
        return 301 {base}/;
    }}

    location {base}/api/ {{
        rewrite ^{base}/api/(.*)$ /$1 break;
        proxy_pass {adapter};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
    }}

    location {base}/ {{
        alias /usr/share/nginx/html/;
        index index.html;
    }}

    location = / {{
        return 301 {base}/;
    }}
}}
"""
else:
    api_base = "/api"
    sse_url = "/api/events/detections"
    html_base = "/"
    nginx = f"""server {{
    listen 80;
    server_name _;

    location = /health {{
        proxy_pass {adapter}/health;
    }}

    location /api/ {{
        rewrite ^/api/(.*)$ /$1 break;
        proxy_pass {adapter};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
    }}

    location / {{
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ /index.html;
    }}
}}
"""

Path("/etc/nginx/conf.d/default.conf").write_text(nginx, encoding="utf-8")

config_js = f"""window.AVIAN_CONFIG = {{
  "apiBase": "{api_base}",
  "sseUrl": "{sse_url}",
  "collageOnly": {str(collage_only).lower()},
  "collageGeneratedOnly": {str(collage_only).lower()},
  "fullRefreshMs": {full_refresh},
  "basePath": "{base or '/'}"
}};
"""
Path("/usr/share/nginx/html/config.js").write_text(config_js, encoding="utf-8")

html_path = Path("/usr/share/nginx/html/index.html")
html = html_path.read_text(encoding="utf-8")
html = re.sub(r"<base href=\"[^\"]*\">\s*", "", html)
html = html.replace("<head>", f'<head>\n<base href="{html_base}">', 1)
if 'src="./config.js"' not in html:
    html = html.replace(
        '<script src="./apt.js"></script>',
        '<script src="./config.js"></script>\n<script src="./apt.js"></script>',
    )
html = re.sub(
    r'<script id="avian-config" type="application/json">.*?</script>\s*'
    r'<script>try\{window\.AVIAN_CONFIG=JSON\.parse\(document\.getElementById\(\'avian-config\'\)\.textContent\|\|\'\{\}\'\);\}catch\(e\)\{window\.AVIAN_CONFIG=\{\};\}</script>',
    '<!-- runtime config: config.js -->',
    html,
    count=1,
    flags=re.DOTALL,
)
if collage_only:
    html = html.replace('class="av-local"', 'class="av-local av-collage-only"', 1)
    html = html.replace('id="themeBtn" type="button" hidden', 'id="themeBtn" type="button"', 1)
html_path.write_text(html, encoding="utf-8")
PY

export ILLUSTRATIONS_DIR="${ILLUSTRATIONS_DIR:-/app/avian/assets/illustrations}"
export GENERATED_DIR="${GENERATED_DIR:-/data/generated}"
export PROMPT_TEMPLATE="${PROMPT_TEMPLATE:-/adapter/prompt.docker.md}"

cd /adapter
python3 -m uvicorn main:app --host 127.0.0.1 --port "${ADAPTER_PORT:-8090}" &
exec nginx -g 'daemon off;'
