FROM python:3.14-slim
WORKDIR /app
# Stockfish for background analysis (§14). Debian installs it at
# /usr/games/stockfish, which is not on PATH in slim images — hence the env
# var below, which pydantic-settings maps onto Settings.stockfish_path.
RUN apt-get update && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*
ENV STOCKFISH_PATH=/usr/games/stockfish
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
EXPOSE 8000
# UV_NO_SYNC: without it, "uv run" re-syncs (and pulls dev deps from PyPI) on every
# container start, defeating the --no-dev build above and requiring network access.
ENV UV_NO_SYNC=1
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
