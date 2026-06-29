# MCP deployment image with ffmpeg for yt-dlp audio post-processing.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && . "$HOME/.local/bin/env" \
 && UV_PROJECT_ENVIRONMENT=/usr/local uv sync --frozen --no-dev --inexact

RUN python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('fastmcp') else 1)"

CMD ["python", "server.py"]
