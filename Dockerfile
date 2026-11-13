FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9 AS base
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f /uv /bin/uv
ENV UV_HTTP_TIMEOUT=300 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/app/cache \
    HF_HOME=/app/cache/huggingface \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable \
    && uv cache clean \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/data /app/logs /app/cache \
    && chown app:app /app/data /app/logs /app/cache
ENV USER=app OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
USER app

FROM base AS collector
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium xvfb xauth util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/collect-city.sh ./scripts/collect-city.sh
USER app
ENTRYPOINT ["bash", "/app/scripts/collect-city.sh"]
CMD ["--help"]

FROM base AS bot
CMD ["python", "-m", "bishkek_food_finder.bot"]
