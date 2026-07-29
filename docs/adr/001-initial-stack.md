# ADR-001: Initial application stack

- **Status:** Accepted
- **Date:** 2026-07-29

## Context

The product is a Windows-local, single-user developer agent that must demonstrate reliable repository RAG, LangGraph orchestration, verifiable citations, cancellation, and security controls within ten working days.

## Decision

Use:

- Next.js with strict TypeScript for the desktop web workspace.
- FastAPI, Pydantic v2, async SQLAlchemy, and Alembic for the application API.
- LangChain components and LangGraph for bounded, observable query orchestration.
- PostgreSQL 16 with pgvector, pg_trgm, and full-text search as the single durable store.
- PostgreSQL-backed jobs and ordered run events; do not add Redis or Celery to the MVP.
- REST for commands and queries, and versioned SSE for ordered run events.
- Native web/API development with PostgreSQL in Docker Compose.

## Consequences

- The system keeps one transactional data store while supporting vector, lexical, and metadata retrieval.
- Python is the primary Agent/RAG implementation language; TypeScript remains focused on interaction quality and contract safety.
- The database is heavier than SQLite, but migration replay and hybrid retrieval better demonstrate the intended engineering standard.
- LangGraph may orchestrate application services but may not own authorization, retrieval correctness, citation trust, or memory persistence.
- Local loopback is the security boundary for the MVP. LAN and public exposure are unsupported.
