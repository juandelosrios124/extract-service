# ──────────────────────────────────────────
# Etapa 1: builder — instala dependencias
# ──────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /service

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev


# ──────────────────────────────────────────
# Etapa 2: production — imagen final mínima
# ──────────────────────────────────────────
FROM python:3.11-slim AS production

WORKDIR /service

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/service/.venv/bin:$PATH" \
    PORT=8001

RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --no-create-home appuser

COPY --from=builder /service/.venv ./.venv
COPY extractor/ ./extractor/

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/health')" \
    || exit 1

CMD ["python", "-m", "extractor"]
