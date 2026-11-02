# syntax=docker/dockerfile:1
# 3Cat Downloader — multi-arch image (linux/amd64, linux/arm64).
# Build: docker build -t 3cat-downloader .
ARG PYTHON_VERSION=3.13

# --------------------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Dependencies first (better layer caching); all of them ship wheels for amd64 and arm64.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md LICENSE ./
COPY tresdl ./tresdl
RUN pip install --no-deps . \
    # The image uses Debian's ffmpeg: drop the ~70 MB binary bundled by imageio-ffmpeg.
    && find /opt/venv -path '*imageio_ffmpeg/binaries/ffmpeg-*' -type f -delete \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim
ARG VERSION=dev
ARG REVISION=unknown
ARG SOURCE=https://github.com/
LABEL org.opencontainers.image.title="3Cat Downloader" \
      org.opencontainers.image.description="Descarrega temporades i sèries senceres de 3cat" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.licenses="MIT"

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh \
    && mkdir -p /config /downloads

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRESDL_DATA_DIR=/config \
    TRESDL_DOWNLOAD_DIR=/downloads \
    TRESDL_HOST=0.0.0.0 \
    TRESDL_PORT=8765 \
    TRESDL_FFMPEG=/usr/bin/ffmpeg \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    TZ=Europe/Madrid

EXPOSE 8765
VOLUME ["/config", "/downloads"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('TRESDL_PORT', '8765'), timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD []
