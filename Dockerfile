# MCP deployment image with ffmpeg for yt-dlp audio post-processing.
# Required environment variables at runtime:
#   GCS_BUCKET                       - Cloud Storage bucket for audio uploads
#   GOOGLE_APPLICATION_CREDENTIALS   - path to service-account key JSON
#                                      (omit when using Workload Identity)
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
RUN python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('google.cloud.storage') else 1)"

CMD ["python", "server.py"]
