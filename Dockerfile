# MCP deployment image with ffmpeg for yt-dlp audio post-processing.
# Required environment variables at runtime:
#   S3_BUCKET              - S3 bucket name for audio uploads
#   AWS_ACCESS_KEY_ID      - AWS credentials (omit when running on EC2/ECS
#   AWS_SECRET_ACCESS_KEY    with an attached IAM role)
#   AWS_REGION             - AWS region of the bucket (default: us-east-1)
#
# Optional environment variables:
#   YTDLP_PROXY            - Outbound proxy for YouTube requests (required on
#                            cloud/datacenter IPs which YouTube blocks).
#                            e.g. http://user:pass@proxy.webshare.io:8080
#   YTDLP_COOKIES_FILE     - Path or S3/HTTPS URL to a Netscape cookies.txt
#                            exported from a browser logged into YouTube.
#                            e.g. s3://your-bucket/cookies.txt
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
RUN python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('youtube_transcript_api') else 1)"

CMD ["python", "server.py"]
