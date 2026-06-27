FROM python:3.12-slim AS adapter

WORKDIR /adapter
COPY adapter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapter/ .

FROM nginx:1.27-alpine

RUN apk add --no-cache python3 py3-pip curl \
    && pip3 install --no-cache-dir --break-system-packages \
        fastapi==0.115.12 uvicorn[standard]==0.34.2 httpx==0.28.1 python-multipart==0.0.20 Pillow==11.2.1

COPY --from=adapter /adapter /adapter

# Frontend static assets + bundled illustrations
COPY avian/frontend /usr/share/nginx/html
COPY avian/assets /app/avian/assets
COPY avian/scripts/prompt.template.md /app/avian/scripts/prompt.template.md

COPY docker/nginx/default.conf /etc/nginx/conf.d/default.conf.template
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /data/generated /var/cache/nginx

ENV BIRDNET_GO_URL=http://host.docker.internal:8180 \
    BASE_PATH=/collage \
    COLLAGE_ONLY=1 \
    ILLUSTRATIONS_DIR=/app/avian/assets/illustrations \
    GENERATED_DIR=/data/generated \
    ADAPTER_PORT=8090

EXPOSE 80
VOLUME ["/data/generated"]

ENTRYPOINT ["/entrypoint.sh"]
