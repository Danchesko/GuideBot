FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y wget && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

ENV UV_HTTP_TIMEOUT=300
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "python", "-m", "bishkek_food_finder.bot"]
