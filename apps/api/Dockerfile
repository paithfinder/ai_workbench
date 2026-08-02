# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY apps/api ./apps/api
# Docling is locked by pyproject.toml/uv.lock. Dependency installation does not
# import the parser or initialize/download its runtime models.
RUN uv sync --frozen --package knowledge-workbench-api --no-dev
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/apps/api/src \
    DOCLING_ARTIFACTS_PATH=/var/cache/docling/models
CMD ["uvicorn", "knowledge_workbench.main:app", "--host", "0.0.0.0", "--port", "8000"]
