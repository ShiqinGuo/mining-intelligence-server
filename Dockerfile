FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12.15-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
COPY deploy ./deploy
RUN chmod +x /app/deploy/backend-entrypoint.sh
CMD ["sh", "/app/deploy/backend-entrypoint.sh"]
