# JobPilot: always-on alternative to running on a laptop.
# Build: docker compose build   Run: docker compose up -d
# Don't run this AND the laptop copy with the same bot token: Telegram allows one poller.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    DATA_DIR=/data PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY jobpilot ./jobpilot
COPY evals ./evals
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 jobpilot && mkdir -p /data && chown jobpilot /data
USER jobpilot
VOLUME ["/data"]
HEALTHCHECK --interval=5m --timeout=20s CMD python -c "import jobpilot" || exit 1
CMD ["python", "-m", "jobpilot", "run"]
