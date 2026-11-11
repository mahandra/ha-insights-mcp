# syntax=docker/dockerfile:1
# HA Insights MCP over Streamable HTTP. Secrets and config are provided at runtime:
#   HA_URL, HA_TOKEN (env), /config/tokens.toml, /config/policy.toml (read-only mounts),
#   /data (writable, audit log).

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first: this layer is cached until uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /config /data && chown app /data
# Only the virtualenv: no uv, no sources, no build tools in the runtime image.
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_TOKENS_PATH=/config/tokens.toml \
    ACTIONS_POLICY_PATH=/config/policy.toml \
    AUDIT_DB_PATH=/data/audit.db
USER app
WORKDIR /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['MCP_PORT']}/healthz\", timeout=3)"]
ENTRYPOINT ["ha-insights-mcp"]
