FROM python:3.11-slim

WORKDIR /app

# uv = fast Python package manager. Pinned minor for reproducibility.
# Must be able to read the lockfile revision written by the local uv (>=0.7).
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv

# Install dependencies from the lockfile only (no project install — the UI is a loose script).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Thin UI client — talks to the backend over HTTP, no LLM/KB in this image.
COPY ui/app.py ./ui/app.py

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Cloud Run injects $PORT; fall back to 8080 for local docker run.
CMD streamlit run ui/app.py \
    --server.port=${PORT:-8080} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
