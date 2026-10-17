# One image for api, worker and cli. Build context: repository root.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.13.0
WORKDIR /app
# dependency layer: only manifests, so source edits do not reinstall third-party packages
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/pyproject.toml
COPY packages/domain/pyproject.toml packages/domain/pyproject.toml
COPY packages/observability/pyproject.toml packages/observability/pyproject.toml
COPY packages/db/pyproject.toml packages/db/pyproject.toml
COPY packages/governance/pyproject.toml packages/governance/pyproject.toml
COPY packages/modelgw/pyproject.toml packages/modelgw/pyproject.toml
COPY packages/graph/pyproject.toml packages/graph/pyproject.toml
COPY packages/ontology/pyproject.toml packages/ontology/pyproject.toml
COPY packages/knowledge/pyproject.toml packages/knowledge/pyproject.toml
COPY packages/jobs/pyproject.toml packages/jobs/pyproject.toml
COPY packages/ingestion/pyproject.toml packages/ingestion/pyproject.toml
COPY packages/extraction/pyproject.toml packages/extraction/pyproject.toml
COPY packages/memory/pyproject.toml packages/memory/pyproject.toml
COPY packages/context/pyproject.toml packages/context/pyproject.toml
COPY packages/reconcile/pyproject.toml packages/reconcile/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY apps/cli/pyproject.toml apps/cli/pyproject.toml
COPY apps/mcp/pyproject.toml apps/mcp/pyproject.toml
COPY sdk/python/pyproject.toml sdk/python/pyproject.toml
COPY sdk/python/README.md sdk/python/README.md
RUN uv sync --frozen --no-dev --no-install-workspace
COPY packages packages
COPY apps/api apps/api
COPY apps/worker apps/worker
COPY apps/cli apps/cli
COPY apps/mcp apps/mcp
COPY sdk/python sdk/python
COPY compat compat
COPY contracts contracts
COPY scripts scripts
RUN uv sync --frozen --no-dev \
 && python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')" 2>/dev/null || true \
 && useradd --system --uid 10001 --create-home kg && chown -R kg /app
ENV PATH="/app/.venv/bin:$PATH" TIKTOKEN_CACHE_DIR=/app/.tiktoken
USER kg
CMD ["uvicorn", "kg_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
