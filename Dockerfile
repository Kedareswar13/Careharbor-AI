# CareHarbor AI container image (API + built React UI on one port).
# Used by docker-compose.yml (VM with Ollama + Caddy HTTPS) and by Hugging Face Spaces (Docker SDK, port 7860).
#   Build:  docker build -t careharbor .
#   Run:    docker run -p 7860:7860 --env-file .env -v careharbor-data:/data careharbor
# All runtime state (clinic DB, chat history, checkpoints, long-term memory, traces) lives in /data:
# mount a volume there or chats are lost when the container is replaced.
# CI (.github/workflows/evals.yml) builds this image and smoke-tests /api/health on every push.

FROM node:20-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 \
    API_HOST=0.0.0.0 API_PORT=7860 APP_ENV=prod DEMO_MODE=true \
    DB_PATH=/data/careharbor.db CHECKPOINT_DB_PATH=/data/checkpoints.db TRACE_DIR=/data/traces \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    PATH="/home/app/careharbor/.venv/bin:$PATH"
RUN pip install --no-cache-dir uv \
    && useradd -m -u 1000 app \
    && mkdir -p /data && chown app:app /data
# Everything below runs as the unprivileged user (Hugging Face requires UID 1000), so the venv and /data are writable.
USER app
WORKDIR /home/app/careharbor
COPY --chown=app:app pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev
COPY --chown=app:app . .
COPY --chown=app:app --from=ui /ui/dist ./frontend/dist
RUN rm -f .env && mkdir -p data \
    && python -c "from careharbor.rag.index import get_index; i = get_index(rebuild=True); print(len(i.docs), 'documents indexed')"
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=4)"
# The venv is on PATH, so plain `python`/`uvicorn` are used (no `uv run`, which would try to re-sync at start-up).
# --if-missing keeps an existing database (bookings + chat history) and only tops up future appointment slots.
# --proxy-headers trusts X-Forwarded-* only from FORWARDED_ALLOW_IPS (default: none outside the container). docker-compose.yml sets it
# for Caddy, which overwrites client-supplied headers. Trusting '*' on a host without such a proxy would let clients spoof their IP.
CMD ["sh", "-c", "python scripts/seed.py --if-missing && exec uvicorn careharbor.api:app --host 0.0.0.0 --port 7860 --proxy-headers"]
