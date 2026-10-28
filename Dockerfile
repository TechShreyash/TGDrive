# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e


FROM --platform=$BUILDPLATFORM node:24.22.0-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5 AS frontend

WORKDIR /frontend
COPY website/package.json website/package-lock.json ./
COPY website/vendor ./vendor
RUN --mount=type=cache,target=/root/.npm npm ci --ignore-scripts
COPY website/ ./
RUN npm run build

FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project


FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    DATA_DIR=/app/data \
    CACHE_DIR=/app/cache

LABEL org.opencontainers.image.title="TGDrive" \
    org.opencontainers.image.description="Self-hosted Telegram-backed personal file drive" \
    org.opencontainers.image.source="https://github.com/TechShreyash/TGDrive" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="2.2.0"

WORKDIR /app

# Distrust the compromised emSign G3 chain in Python/MongoDB system TLS too.
# https://github.com/astral-sh/uv/security/advisories/GHSA-hq85-5cpj-mr62
RUN apt-get update \
    && apt-get install -y --no-install-recommends libarchive13t64 ffmpeg poppler-utils aria2 \
    && sed -i 's|^mozilla/emSign_ECC_Root_CA_-_G3.crt$|!mozilla/emSign_ECC_Root_CA_-_G3.crt|' /etc/ca-certificates.conf \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 tgdrive \
    && useradd --uid 10001 --gid tgdrive --no-create-home --shell /usr/sbin/nologin tgdrive \
    && mkdir -p /app/data /app/cache \
    && chown -R tgdrive:tgdrive /app/data /app/cache

COPY --from=builder /app/.venv /app/.venv
COPY --chown=tgdrive:tgdrive LICENSE ./
COPY --chown=tgdrive:tgdrive tgdrive ./tgdrive
COPY --from=frontend --chown=tgdrive:tgdrive /frontend/dist ./website/dist

USER 10001:10001

EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

CMD ["/app/.venv/bin/python", "-m", "tgdrive.server"]
