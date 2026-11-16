FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project

COPY config ./config
COPY src ./src
RUN uv sync --locked

RUN useradd --system --create-home --uid 10001 tickbook \
    && mkdir -p /app/data /app/reports \
    && chown -R tickbook:tickbook /app/data /app/reports
USER tickbook
VOLUME ["/app/data"]

ENTRYPOINT ["mm"]
CMD ["record"]
