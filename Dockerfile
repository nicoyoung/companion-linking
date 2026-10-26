FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV COMPANION_DATABASE_PATH=/data/companion.db
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "companion.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
