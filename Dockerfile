FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    WS_DATA_DIR=/data

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY watchlist_sync ./watchlist_sync
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --no-create-home app && mkdir /data && chown app /data
USER 10001
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]
CMD ["/app/.venv/bin/python", "-m", "watchlist_sync"]
