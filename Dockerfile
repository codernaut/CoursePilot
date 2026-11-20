# syntax=docker/dockerfile:1
#
# Optional container image for CoursePilot (deployment profile 3b, see docs/deploy.md guide E and
# deploy/kubernetes/). Docker is never required: setup.*, start.* and the service install do not use it.
#
# Build from the repository root:   docker build -t coursepilot:local .
# One image, two roles (same image, different command):
#   web     python -m app.cli serve      (migrates first, then serves on 0.0.0.0:8000)
#   worker  python -m app.worker         (parses and embeds uploads; same /data volume as web)
#
# Configuration comes only from environment variables (see .env.example). Data lives in /data.
# OCR (tesseract) is not installed because OCR_ENABLED is off by default. To add it, extend this
# file: in the runtime stage run `apt-get install -y --no-install-recommends tesseract-ocr` and
# `uv pip install pytesseract` in the builder, then set OCR_ENABLED=true.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
# Optional dependency extras, space separated. Empty = the normal image. In-process embeddings
# (EMBEDDING_PROVIDER=local) need PyTorch (a very large layer):
#   docker compose build --build-arg EXTRAS=local-embeddings
# Chat and embeddings through an Ollama or OpenAI-compatible server need no extra.

# ---- builder: resolve and install the locked runtime dependencies --------------------------------
FROM ${PYTHON_IMAGE} AS builder

ARG EXTRAS=""

# uv.lock is written by a recent uv (lock revision 5); an old pinned uv may refuse it. CI uses the
# latest uv too. The lock file, not uv, pins the dependencies.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system

# The virtualenv is created inside backend/ and must live at the same path in the runtime stage.
WORKDIR /app/backend

# Dependencies first, so this layer is cached until pyproject.toml or uv.lock change.
COPY backend/pyproject.toml backend/uv.lock ./
RUN flags=""; for extra in $EXTRAS; do flags="$flags --extra $extra"; done;     uv sync --frozen --no-dev --no-install-project $flags

# Then the application (the project itself is not a package: `package = false`).
COPY backend/ ./
RUN flags=""; for extra in $EXTRAS; do flags="$flags --extra $extra"; done;     uv sync --frozen --no-dev $flags

# ---- runtime ------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ARG GIT_COMMIT=""

ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    DATA_DIR=/data \
    DATABASE_URL=sqlite+aiosqlite:////data/coursepilot.db \
    CHECKPOINT_DB_PATH=/data/checkpoints.db \
    PATH=/app/backend/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user. REPO_ROOT resolves to /app, so .env (absent in the container) would be /app/.env.
RUN groupadd --system --gid 10001 coursepilot \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin coursepilot \
    && mkdir -p /data \
    && chown 10001:10001 /data

WORKDIR /app/backend

COPY --from=builder --chown=10001:10001 /app/backend /app/backend
COPY .env.example LICENSE THIRD_PARTY_NOTICES.md /app/

# VERSION ("<version> <full sha>") is what GET /api/v1/health/version reads; only written when
# the build passes --build-arg GIT_COMMIT=$(git rev-parse HEAD).
RUN if [ -n "$GIT_COMMIT" ]; then echo "docker $GIT_COMMIT" > /app/VERSION; fi

USER 10001:10001

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=4).status == 200 else 1)"]

# Exec form: python is PID 1 and receives SIGTERM directly (uvicorn and the worker both shut down
# gracefully on it).
CMD ["python", "-m", "app.cli", "serve"]
