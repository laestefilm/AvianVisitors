# Debian-based final image — Alpine's Python 3.12 + pip hits a broken pyexpat.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx curl \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default \
    && mkdir -p /data/generated /data/frame-export /var/cache/nginx /app/avian/scripts

WORKDIR /adapter
COPY docker/adapter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY docker/adapter/ .

COPY avian/frontend /usr/share/nginx/html
COPY avian/assets /app/avian/assets
COPY avian/scripts/prompt.template.md /app/avian/scripts/prompt.template.md

COPY docker/nginx/default.conf /etc/nginx/conf.d/default.conf.template
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV BIRDNET_GO_URL=http://host.docker.internal:8180 \
    BASE_PATH=/collage \
    COLLAGE_ONLY=1 \
    ILLUSTRATIONS_DIR=/app/avian/assets/illustrations \
    GENERATED_DIR=/data/generated \
    ADAPTER_PORT=8090

EXPOSE 80
VOLUME ["/data/generated"]

ENTRYPOINT ["/entrypoint.sh"]
