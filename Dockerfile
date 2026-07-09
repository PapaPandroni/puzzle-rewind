FROM python:3.14-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
EXPOSE 8000
# TODO Phase 3: install the Stockfish binary here (apt-get install stockfish or a
# static build) once self-hosted engine analysis is added.
# UV_NO_SYNC: without it, "uv run" re-syncs (and pulls dev deps from PyPI) on every
# container start, defeating the --no-dev build above and requiring network access.
ENV UV_NO_SYNC=1
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
