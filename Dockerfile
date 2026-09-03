# syntax=docker/dockerfile:1

# --- build: resolve dependencies into a virtualenv ---------------------------
FROM python:3.14-slim AS builder

# Pinned to the uv that wrote uv.lock, so the image resolves the same tree
# the developers tested. `:latest` here would make builds drift silently.
COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /bin/uv

WORKDIR /app

# Bytecode is compiled once here rather than on every container start.
# UV_PYTHON_DOWNLOADS=never keeps uv on the interpreter this image already has.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Only the two files that decide the dependency set, so the layer is reused
# whenever application code changes and the lockfile does not.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# --- run ---------------------------------------------------------------------
FROM python:3.14-slim AS runtime

# Unbuffered so log lines reach the platform as they happen, not at flush time.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Nothing here needs root, and the decision journal needs a writable directory
# that belongs to the same user.
RUN useradd --create-home --uid 10001 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/ ./src/
COPY --chown=app:app config/ ./config/

RUN mkdir -p /app/data && chown app:app /app/data

USER app

EXPOSE 8000

# Container Apps runs its own probes and ignores this. It is here for local
# runs and for `docker compose`.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import httpx; httpx.get('http://127.0.0.1:8000/health-check').raise_for_status()"]

# One worker, deliberately. A second one would run its own Graph subscription
# loop and its own in-memory dedupe, so the two would delete each other's
# subscription and forward the same RFQ to a customer's desk twice.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
