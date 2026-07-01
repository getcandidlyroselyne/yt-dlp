# MCP deployment image with ffmpeg for yt-dlp audio post-processing.
# Required environment variables at runtime:
#   S3_BUCKET              - S3 bucket name for audio uploads
#   AWS_ACCESS_KEY_ID      - AWS credentials (omit when running on EC2/ECS
#   AWS_SECRET_ACCESS_KEY    with an attached IAM role)
#   AWS_REGION             - AWS region of the bucket (default: us-east-1)
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
 && UV_PROJECT_ENVIRONMENT=/usr/local uv sync --frozen --no-dev --inexact --extra curl-cffi

RUN python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('fastmcp') else 1)"
RUN python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('boto3') else 1)"

CMD ["python", "server.py"]
