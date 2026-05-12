FROM node:22-bookworm-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    DATA_DIR=/app/data \
    RUN_SCHEDULER_IN_WEB=true

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev

COPY backend ./backend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

RUN mkdir -p /app/data

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3).read()" || exit 1

CMD ["uv", "run", "python", "-m", "backend.app"]
