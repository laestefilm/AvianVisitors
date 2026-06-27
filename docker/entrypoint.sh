#!/bin/sh
set -eu

BASE_PATH="${BASE_PATH:-/collage}"
BASE_PATH="$(echo "$BASE_PATH" | sed 's#/*$##')"
[ -z "$BASE_PATH" ] && BASE_PATH="/collage"

sed "s#__BASE_PATH__#${BASE_PATH}#g" /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf

API_BASE="${BASE_PATH}/api"
SSE_URL="${BASE_PATH}/api/events/detections"
FULL_REFRESH_MS="${FULL_REFRESH_MS:-300000}"
COLLAGE_ONLY="${COLLAGE_ONLY:-true}"

# Runtime config consumed by apt.js (loaded before apt.js in index.html).
cat > /usr/share/nginx/html/config.js <<EOF
window.AVIAN_CONFIG = {
  "apiBase": "${API_BASE}",
  "sseUrl": "${SSE_URL}",
  "collageOnly": ${COLLAGE_ONLY},
  "fullRefreshMs": ${FULL_REFRESH_MS},
  "basePath": "${BASE_PATH}"
};
EOF

# Ensure index.html has <base href> and loads config.js (idempotent).
python3 - <<PY
import re
from pathlib import Path

html_path = Path("/usr/share/nginx/html/index.html")
html = html_path.read_text(encoding="utf-8")
base = "${BASE_PATH}".rstrip("/") + "/"

if "<base " not in html:
    html = html.replace("<head>", f'<head>\n<base href="{base}">', 1)

if 'src="./config.js"' not in html:
    html = html.replace(
        '<script src="./apt.js"></script>',
        '<script src="./config.js"></script>\n<script src="./apt.js"></script>',
    )

# Drop legacy inline JSON config (config.js is authoritative in Docker).
html = re.sub(
    r'<script id="avian-config" type="application/json">.*?</script>\s*'
    r'<script>try\{window\.AVIAN_CONFIG=JSON\.parse\(document\.getElementById\(\'avian-config\'\)\.textContent\|\|\'\{\}\'\);\}catch\(e\)\{window\.AVIAN_CONFIG=\{\};\}</script>',
    '<!-- runtime config: config.js -->',
    html,
    count=1,
    flags=re.DOTALL,
)

if COLLAGE_ONLY := ("${COLLAGE_ONLY}" == "1" or "${COLLAGE_ONLY}".lower() == "true"):
    html = html.replace('class="av-local"', 'class="av-local av-collage-only"', 1)

html_path.write_text(html, encoding="utf-8")
PY

export ILLUSTRATIONS_DIR="${ILLUSTRATIONS_DIR:-/app/avian/assets/illustrations}"
export GENERATED_DIR="${GENERATED_DIR:-/data/generated}"
export PROMPT_TEMPLATE="${PROMPT_TEMPLATE:-/app/avian/scripts/prompt.template.md}"

cd /adapter
python3 -m uvicorn main:app --host 127.0.0.1 --port "${ADAPTER_PORT:-8090}" &
exec nginx -g 'daemon off;'
