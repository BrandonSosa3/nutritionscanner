# syntax=docker/dockerfile:1.7
# Three stages. `runtime` is what deploys: no build toolchain, no uv, no dev
# dependencies, non-root. `dev` adds the tooling needed to generate migrations
# and run tests, and is what compose builds locally.
#
# Every stage installs from uv.lock with --frozen, so the dependency set is
# byte-identical between a laptop, CI, and production. Resolving from
# pyproject.toml at build time instead would let a transitive dependency drift
# between environments without anything changing in git.

# ── Builder ───────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve in their own layer, so editing application code does
# not invalidate the dependency cache.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
# --no-editable: the package is copied into site-packages rather than linked
# to /app/src, so the runtime image does not depend on the source tree layout.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ── Dev builder ───────────────────────────────────────────────────────────
# Same lock, plus dev extras. Alembic's post-write hook shells out to ruff,
# so migration generation needs them present.
FROM builder AS dev-builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --no-editable

# ── Runtime base ──────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUFF_CACHE_DIR=/tmp/.ruff_cache

WORKDIR /app

# Receipt images land here. Owned by the app user so the container writes
# without privilege; a volume mounts over it locally and object storage
# replaces it in production.
RUN mkdir -p /app/data/receipts && chown -R app:app /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "ns.api.app:app", "--host", "0.0.0.0", "--port", "8000"]

# ── Production runtime ────────────────────────────────────────────────────
FROM base AS runtime
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/alembic /app/alembic
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
USER app

# ── Development runtime ───────────────────────────────────────────────────
FROM base AS dev
COPY --from=dev-builder --chown=app:app /app/.venv /app/.venv
COPY --from=dev-builder --chown=app:app /app/src /app/src
COPY --from=dev-builder --chown=app:app /app/alembic /app/alembic
COPY --from=dev-builder --chown=app:app /app/alembic.ini /app/alembic.ini
USER app
