# AI Workbench

A local-first developer Agent workspace for understanding TypeScript and Python repositories with Agentic RAG, observable LangGraph execution, and verifiable source citations.

> Current status: **Day 2 repository authorization and secure manifest ingestion**. Chunking, embedding, retrieval, verified citations, and LangGraph query runs begin in the next vertical slice.

## Principles

- Read-only by default; no shell, code execution, or Git writes in the MVP.
- Explicit per-repository authorization.
- Retrieved content is untrusted data.
- Facts require server-verified citations.
- Model context is minimized, redacted, and audited.
- A disconnected stream is not a successful run.

## Stack

- Next.js, React, strict TypeScript
- FastAPI, Pydantic, async SQLAlchemy, Alembic
- LangChain and LangGraph
- PostgreSQL 16, pgvector, pg_trgm, full-text search
- OpenTelemetry-compatible structured observability

## Prerequisites

- Node.js 20.19+
- pnpm 10+
- Python 3.12+
- uv
- Docker Desktop with Compose (required for PostgreSQL)

Docker was not available in the initial implementation environment, so database-dependent acceptance items—including the real migration replay and PostgreSQL integration checks—must be verified on a machine with Docker or in hosted CI.

## Setup

1. Create the local environment file and replace the database password. Never commit it.

```bash
cp .env.example .env
```

2. Install dependencies.

```bash
pnpm install
```

```bash
uv sync --project apps/api --extra dev
```

3. Start PostgreSQL.

```bash
docker compose --env-file .env -f infra/compose/docker-compose.yml up -d postgres
```

4. Apply migrations.

```bash
uv run --project apps/api alembic upgrade head
```

5. Start the API on loopback only.

```bash
uv run --project apps/api uvicorn ai_workbench_api.main:app --app-dir apps/api/src --host 127.0.0.1 --port 8000 --reload
```

6. In another terminal, start the web workspace.

```bash
pnpm dev:web
```

Open <http://127.0.0.1:3000>.

## Verification

```bash
pnpm check
```

On Windows, the same checks are available through `scripts/verify.ps1`.

## Documentation

- [Architecture](docs/architecture/overview.md)
- [Initial stack ADR](docs/adr/001-initial-stack.md)
- [Threat model](docs/security/threat-model.md)
- [Day 1 acceptance](docs/user-guide/day-1-acceptance.md)
- [Day 2 acceptance](docs/user-guide/day-2-acceptance.md)

## Supported deployment boundary

The MVP supports one user on one trusted Windows workstation. The API and database are loopback-only. LAN exposure, public hosting, multi-user authentication, and remote execution are explicitly unsupported.
