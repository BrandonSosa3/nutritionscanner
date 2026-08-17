# syntax=docker/dockerfile:1.7
# Multi-stage. The runtime image carries no build toolchain and no uv, runs as
# a non-root user, and is byte-identical between local dev and production —
# only the command and environment differ.

# ── Builder ───────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve in their own layer so application edits don't
# invalidate the dependency cache.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    VIRTUAL_ENV=/app/.venv uv pip install -r pyproject.toml --extra dev

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/app/.venv uv pip install --no-deps -e .

# ── Runtime ───────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# curl is used by the container healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/alembic /app/alembic
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini

# Receipt images land here. Owned by the app user so the container can write
# without running privileged; mount a volume over it in compose and a real
# object store in production.
RUN mkdir -p /app/data/receipts && chown -R app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "ns.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
