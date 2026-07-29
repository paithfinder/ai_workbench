# Architecture Overview

AI Workbench is a local-first, single-user developer agent. The first release uses a modular monorepo rather than microservices.

## Runtime topology

```text
Browser → Next.js web (127.0.0.1:3000)
                ↓ typed HTTP / SSE
          FastAPI (127.0.0.1:8000)
                ↓ async SQLAlchemy
   PostgreSQL 16 + pgvector (Docker, loopback only)
```

The API and web processes run natively for fast development. PostgreSQL is the only Day 1 containerized dependency. A dedicated worker entry point will be introduced with indexing runs; Redis and Celery are intentionally excluded from the MVP.

## Boundaries

- `apps/web`: presentation and typed API consumption. It never grants filesystem access by itself.
- `apps/api/src/ai_workbench_api/api`: HTTP/SSE transport and validation.
- `apps/api/src/ai_workbench_api/domain`: framework-independent contracts and domain policy.
- `apps/api/src/ai_workbench_api/security`: source authorization, redaction, outbound policy, and audit controls.
- `apps/api/src/ai_workbench_api/graphs`: LangGraph orchestration. Domain services must remain usable without LangGraph.
- `apps/api/src/ai_workbench_api/rag`: indexing, retrieval, rank fusion, and citation verification.
- `apps/api/src/ai_workbench_api/sources`: read-only source adapters.
- `apps/api/src/ai_workbench_api/worker`: leased database jobs and cooperative cancellation.
- `apps/api/src/ai_workbench_api/db`: SQLAlchemy persistence models and sessions.
- `packages/api-contract`: generated TypeScript contract artifacts from FastAPI OpenAPI.

## Architectural rules

1. Only the FastAPI service may access authorized local repositories.
2. Retrieved content is untrusted data, never executable instruction.
3. Model-produced citations are untrusted until the server verifies source version, range, and excerpt hash.
4. Read-only tools have typed schemas, fixed allowlists, timeouts, result budgets, and audit wrappers.
5. Run completion is an explicit backend terminal event; an SSE disconnect is not success.
6. Secrets are never persisted as plaintext business data and never logged.
