# ──────────────────────────────────────────
# Etapa 1: builder — instala dependencias
# ──────────────────────────────────────────
FROM python:3.11-slim@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79 AS builder

WORKDIR /service

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev


# ──────────────────────────────────────────
# Etapa 2: production — imagen final mínima
# ──────────────────────────────────────────
FROM python:3.11-slim@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79 AS production

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
