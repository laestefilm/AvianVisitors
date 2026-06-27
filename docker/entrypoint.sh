#!/bin/sh
set -eu

BASE_PATH="${BASE_PATH:-/collage}"
BASE_PATH="$(echo "$BASE_PATH" | sed 's#/*$##')"
[ -z "$BASE_PATH" ] && BASE_PATH="/collage"

# nginx config: inject base path
sed "s#__BASE_PATH__#${BASE_PATH}#g" /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf

API_BASE="${BASE_PATH}/api"
SSE_URL="${BASE_PATH}/api/events/detections"
FULL_REFRESH_MS="${FULL_REFRESH_MS:-300000}"

cat > /usr/share/nginx/html/avian-config.json <<EOF
{
  "apiBase": "${API_BASE}",
  "sseUrl": "${SSE_URL}",
  "collageOnly": ${COLLAGE_ONLY:-true},
  "fullRefreshMs": ${FULL_REFRESH_MS},
  "basePath": "${BASE_PATH}"
}
EOF

# Inject runtime config into index.html
python3 - <<'PY'
import json
from pathlib import Path

cfg = json.loads(Path("/usr/share/nginx/html/avian-config.json").read_text())
html = Path("/usr/share/nginx/html/index.html").read_text(encoding="utf-8")
snippet = json.dumps(cfg)
html = html.replace(
    '<script id="avian-config" type="application/json">{}</script>',
    f'<script id="avian-config" type="application/json">{snippet}</script>',
)
if cfg.get("basePath"):
    base = cfg["basePath"].rstrip("/") + "/"
    if "<base " not in html:
        html = html.replace("<head>", f'<head>\n<base href="{base}">', 1)
Path("/usr/share/nginx/html/index.html").write_text(html, encoding="utf-8")
PY

export ILLUSTRATIONS_DIR="${ILLUSTRATIONS_DIR:-/app/avian/assets/illustrations}"
export GENERATED_DIR="${GENERATED_DIR:-/data/generated}"
export PROMPT_TEMPLATE="${PROMPT_TEMPLATE:-/app/avian/scripts/prompt.template.md}"

if [ -n "${WANGP_ROOT:-}" ] && [ -d "$WANGP_ROOT" ]; then
  echo "WanGP root mounted at $WANGP_ROOT"
fi

cd /adapter
python3 -m uvicorn main:app --host 127.0.0.1 --port "${ADAPTER_PORT:-8090}" &
exec nginx -g 'daemon off;'
