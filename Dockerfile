FROM ghcr.io/astral-sh/uv:0.12.8 AS uv
FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /opt/bridge
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
ENV PATH="/opt/bridge/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATABASE_PATH=/data/bridge.db
RUN groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin bridge \
    && install -d -o bridge -g bridge -m 0700 /data
WORKDIR /opt/bridge
COPY --from=builder /opt/bridge/.venv ./.venv
USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["python", "-m", "app"]
