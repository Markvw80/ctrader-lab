FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ARG PUID=1000
ARG PGID=1000
ARG GIT_COMMIT=unknown
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CTLAB_GIT_COMMIT=${GIT_COMMIT}

WORKDIR /app

# Dependencies first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY config ./config
COPY README.md ./
RUN uv sync --frozen --no-dev

RUN groupadd -g ${PGID} ctlab && useradd -u ${PUID} -g ${PGID} -m ctlab \
    && mkdir -p /data /results && chown ctlab:ctlab /data /results
USER ctlab

ENV CTLAB_DATA_DIR=/data CTLAB_RESULTS_DIR=/results CTLAB_CONFIG_DIR=/app/config
EXPOSE 8088
CMD ["sh", "-c", "uvicorn ctlab.web.app:app --host 0.0.0.0 --port 8088 --workers 1"]
