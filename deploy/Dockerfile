# Boardy — multi-stage Dockerfile.
#
# Lives in deploy/, but `deploy/docker-compose.yml` sets the build context to
# the REPO ROOT (`context: ..`) — so all COPY paths below are relative to the
# project root, not to deploy/. Lets us COPY pyproject.toml/uv.lock without
# any `../` gymnastics.
#
# Stage 1 (builder): full Python image, installs uv, syncs deps to a project
# venv, and pre-downloads the multilingual-e5-base sentence-transformer model
# (~280MB). Pre-caching the model in the image avoids a cold 30-60s download
# on first container start, at the cost of a fatter image. Worth it for a
# deploy-and-forget workflow where the image is built once on the server.
#
# Stage 2 (runtime): slim image, copies the .venv and the Hugging Face cache
# from the builder. No toolchain at runtime. Final image ~1.5GB.
#
# What's NOT in the image (mounted at runtime as volumes / files):
#   - app/ etl/ web/      (bind-mounted, so `git pull && docker compose
#                         -f deploy/docker-compose.yml restart boardy`
#                         updates Python without rebuild)
#   - boardy.db           (named volume, persists across container rebuilds)
#   - rulebooks/          (named volume, persists PDF binaries)
#   - .env                (mounted read-only, contains the API keys)
#
# Why bind-mount the code instead of COPYing it: this is a personal app that
# the owner iterates on weekly. Rebuilding the image on every code change is
# pure waste. Dependency changes (rare) DO require a rebuild — that's the
# point of `pyproject.toml` being COPYed in.

ARG PYTHON_VERSION=3.13

# ─────────────────────────────────────────────────────────────────────────
# Stage 1: builder — uv + deps + e5 model cache
# ─────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

# Install uv (fast, deterministic Python package manager — same tool the
# owner uses locally, so dev/prod parity).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# uv defaults that make the build reproducible and fast.
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Copy the lock files FIRST and resolve deps before copying source. This
# leverages Docker's layer cache: if pyproject.toml + uv.lock didn't change,
# the (long) `uv sync` layer is reused even when app code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Pre-download the multilingual-e5-base model so the first container boot
# doesn't spend a minute talking to Hugging Face. The HF_HOME env tells
# sentence-transformers where to cache; we'll copy this dir into the
# runtime stage. The download is ~1.1GB unpacked, but the layer is content-
# addressable so identical model versions reuse the same blob.
ENV HF_HOME=/opt/hf-cache
RUN mkdir -p ${HF_HOME} && \
    .venv/bin/python -c "from sentence_transformers import SentenceTransformer; \
                          SentenceTransformer('intfloat/multilingual-e5-base')"

# ─────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — slim image, no toolchain
# ─────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# Non-root user — uvicorn doesn't need root, and running as root inside a
# container is the lazy default that catches you out the day you forget to
# fix permissions on a bind-mount.
RUN groupadd --gid 1000 boardy && \
    useradd --uid 1000 --gid boardy --shell /bin/bash --create-home boardy

WORKDIR /app

# Copy the venv + HF cache from builder.
COPY --from=builder --chown=boardy:boardy /app/.venv /app/.venv
COPY --from=builder --chown=boardy:boardy /opt/hf-cache /home/boardy/.cache/huggingface

# Source code (app/, etl/, web/) is bind-mounted via docker-compose for
# dev-style updates. We still COPY pyproject.toml so `python -m` lookups
# inside the venv resolve correctly (uv writes `pyproject.toml` references).
COPY --chown=boardy:boardy pyproject.toml ./

# Put venv on PATH so `uvicorn` etc. resolve without `.venv/bin/` prefix.
ENV PATH="/app/.venv/bin:${PATH}" \
    HF_HOME=/home/boardy/.cache/huggingface \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1

USER boardy
EXPOSE 8765

# Healthcheck: the index page returns the SPA shell, used as a liveness probe.
# 5s/30s/3 retries keeps the container marked unhealthy fast if the process
# wedges, but tolerates a slow first request (cold model load).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
