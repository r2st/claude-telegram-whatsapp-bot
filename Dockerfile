# API-mode image. CLI mode needs the `claude` binary and its host auth, which
# don't belong in a container — set CLAUDE_MODE=api and ANTHROPIC_API_KEY.
#
# This file used to COPY loose modules from the repository root — main.py,
# claude_core.py, telegram_bot.py, …, plus a bot.py that has never existed. They
# all live in telechat_pkg/ now, so every `docker compose up` failed at build
# time on a path the README documents as supported. Installing the package is
# what keeps that from rotting again: the sources of truth are pyproject.toml and
# the package directory, not a hand-maintained file list.
FROM python:3.12-slim

# API mode needs the httpx and docs extras it actually imports; `all` would pull
# playwright (a browser stack this image has no use for).
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TELECHAT_HOME=/data \
    HEALTH_PORT=8484 \
    HEALTH_BIND_ADDR=0.0.0.0

WORKDIR /app

# Only what the wheel build needs: pyproject.toml, the README its `readme` field
# points at, and the package itself.
COPY pyproject.toml README.md ./
COPY telechat_pkg/ ./telechat_pkg/
RUN pip install --no-cache-dir ".[httpx,docs]"

# Run as a non-root user, and keep the database on a volume so an image rebuild
# doesn't take the conversation history with it.
RUN useradd --create-home --uid 10001 telechat \
    && mkdir -p /data \
    && chown -R telechat:telechat /data /app
USER telechat
VOLUME ["/data"]

EXPOSE 8484

# The health server already reports component status and breaker state; wiring it
# up here means a wedged bot shows as unhealthy instead of merely "running".
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8484/health', timeout=4).status == 200 else 1)"

CMD ["telechat", "start"]
