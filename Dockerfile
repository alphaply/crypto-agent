ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.11-slim-trixie

FROM ${NODE_IMAGE} AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm ci --registry=${NPM_REGISTRY}
COPY frontend/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    DATA_DIR=/app/data \
    RUN_SCHEDULER_IN_WEB=true

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev

COPY backend ./backend
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

RUN mkdir -p /app/data

ARG IMAGE_REVISION=unverified
ENV IMAGE_REVISION=${IMAGE_REVISION}

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3))['success']" || exit 1

CMD ["uv", "run", "--no-sync", "python", "-m", "backend.app"]
