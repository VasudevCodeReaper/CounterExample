# Counterexample MCP server (streamable HTTP) with Python, g++ and git.
#   docker build -t counterexample .
#   docker run --rm -p 8000:8000 -e COUNTEREXAMPLE_API_TOKEN=<long-random-secret> counterexample
#   MCP endpoint: http://localhost:8000/mcp  (clients send "Authorization: Bearer <secret>")
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE SKILL.md ./
COPY counterexample ./counterexample
RUN pip install ".[fastapi]"

RUN useradd --create-home --uid 10001 cx \
    && git config --system --add safe.directory '*'
USER cx

ENV MCP_TRANSPORT=streamable-http \
    HOST=0.0.0.0 \
    PORT=8000 \
    SANDBOX_TIMEOUT_MS=2000 \
    DEFAULT_ARENA_RUNS=5000 \
    COUNTEREXAMPLE_CACHE=/tmp/counterexample-cache

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

ENTRYPOINT ["counterexample-mcp"]
