FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12.15-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/backend/pyproject.toml ./apps/backend/pyproject.toml
COPY apps/mcp_gateway/pyproject.toml ./apps/mcp_gateway/pyproject.toml
COPY packages/contracts ./packages/contracts
COPY apps/mcp_gateway/src ./apps/mcp_gateway/src
RUN uv sync --frozen --package mining-gateway --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "uvicorn", "mining_gateway.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8001"]
