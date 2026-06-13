# services/language/Dockerfile
# Base: kleiner Debian-Slim mit Python 3.12
FROM python:3.12-slim AS runtime

# ----- Build-Args für Metadaten -----
ARG BUILD_VERSION=0.1.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

# ----- OCI Labels -----
LABEL org.opencontainers.image.title="translate-svc" \
      org.opencontainers.image.description="Flask microservice wrapper for LibreTranslate" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="." \
      org.opencontainers.image.vendor="hyatlas" \
      org.opencontainers.image.licenses="MIT"

# ----- System-Setup -----
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin

# Systempakete: Zertifikate, tzdata, curl für Healthcheck, bash für entrypoint.sh
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates tzdata curl bash && \
    rm -rf /var/lib/apt/lists/* && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && echo ${TZ} > /etc/timezone

# ----- Verzeichnis und User -----
WORKDIR /app
# Non-root Nutzer
RUN groupadd -g 10001 app && useradd -g app -u 10001 -m -s /usr/sbin/nologin app

# ----- Dependencies zuerst (Layer-Caching) -----
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# ----- App-Code -----
COPY . /app

# entrypoint ausführbar machen (robust, auch wenn Datei ggf. fehlt)
RUN chmod +x /app/entrypoint.sh || true

# ----- Laufzeit-Defaults -----
ENV PORT=8000 \
    LOG_LEVEL=INFO \
    LT_BASE_URL=http://libretranslate:5000 \
    HTTP_TIMEOUT_SEC=30 \
    MAX_CHARS=10000 \
    MAX_TARGETS=10 \
    ALLOWED_LANGS="en,de,fr,es,pt,ru,zh-CN,ja,ko,tr,pl,it,nl,ar,id,cs,uk,sv" \
    SERVICE_NAME=translate-svc \
    APP_VERSION=${BUILD_VERSION} \
    PYTHONPATH=/app

EXPOSE 8000

# Healthcheck gegen lokalen Endpoint
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=12 \
  CMD curl -fsS http://127.0.0.1:${PORT}/_health || exit 1

# Sicherheit: besitze App-Dateien
RUN chown -R app:app /app
USER app

# Start:
# - ENTRYPOINT: unser Bash-Skript (Upstream-Wait, Redis-Probe, Preload wsgi.app, Start gunicorn)
# - CMD: Standard-Gunicorn-Befehl, den entrypoint.sh verwendet
ENTRYPOINT ["bash", "/app/entrypoint.sh"]
CMD ["gunicorn","-c","gunicorn.conf.py","wsgi:app"]
