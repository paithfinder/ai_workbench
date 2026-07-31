# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY apps/api ./apps/api
RUN uv sync --frozen --package knowledge-workbench-api --no-dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/apps/api/src
CMD ["uvicorn", "knowledge_workbench.main:app", "--host", "0.0.0.0", "--port", "8000"]
