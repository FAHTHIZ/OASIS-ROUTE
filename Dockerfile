# syntax=docker/dockerfile:1.7
# Oasis Route: API + gateway + console, with the background worker supervised in-process by default.

FROM python:3.15-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project --extra otel
COPY src ./src
RUN uv sync --locked --no-dev --extra otel --no-editable

FROM python:3.15-slim AS runtime
# Security updates for the base OS; drop the unused system installer tooling from the final image.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
RUN python -m pip uninstall -y pip setuptools wheel || true
RUN groupadd --system --gid 10001 oasis \
 && useradd --system --uid 10001 --gid oasis --home-dir /app --shell /usr/sbin/nologin oasis \
 && install -d -o oasis -g oasis /data
WORKDIR /app
COPY --from=build --chown=oasis:oasis /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OASIS_ROUTE_HOST=0.0.0.0 \
    OASIS_ROUTE_PORT=8080 \
    OASIS_ROUTE_ENVIRONMENT=production \
    OASIS_ROUTE_WORKER_MODE=embedded \
    OASIS_ROUTE_DEV_STATE_DIR=/data/dev
USER oasis
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["oasis-route"]
CMD ["serve"]
