FROM python:3.11-slim

WORKDIR /app

# uv = fast Python package manager. Pinned minor for reproducibility.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Install dependencies from the lockfile only (no project install — we don't need a build-system).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# App code.
COPY app.py assistant.py kb.py system_prompt.md ./
COPY knowledge_base/ ./knowledge_base/

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Cloud Run injects $PORT; fall back to 8080 for local docker run.
CMD streamlit run app.py \
    --server.port=${PORT:-8080} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
