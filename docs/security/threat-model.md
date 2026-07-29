# MVP Threat Model

## Protected assets

- Local source code and private documents.
- OpenAI, GitHub, and Notion credentials.
- Conversation history, memory, citations, and audit records.
- Integrity of repository authorization and run terminal states.

## Trust boundaries

1. Browser to loopback Web/API.
2. FastAPI to explicitly authorized repository roots.
3. FastAPI to PostgreSQL.
4. FastAPI to external model and source APIs.
5. Retrieved source content to LangGraph and model prompts.

## Day 1 controls

- API and database ports bind to loopback only.
- CORS accepts only configured local web origins.
- No default user, password, JWT secret, or provider key.
- Environment files, private keys, credential files, generated data, and logs are ignored by Git.
- Configuration can start without a model key and reports only a boolean configuration state.
- A shared redaction layer removes recognizable provider tokens before logs and errors are emitted.
- Database migrations are explicit and replayable; the application does not silently create schema at startup.

## Required before repository ingestion

- Canonical path checks after resolving Windows junctions and symbolic links.
- Per-repository durable authorization and revocation.
- Deny rules for `.env`, credentials, private keys, VCS internals, dependency trees, binaries, and oversized files.
- Audit records for authorization, scanning, indexing, outbound model context, and deletion.

## Required before URL ingestion

- Scheme allowlist, DNS resolution, IP classification, redirect-by-redirect validation, response-size and MIME limits, and timeouts.

## Explicitly unavailable in the MVP

- Shell and subprocess tools.
- Code execution.
- Git writes.
- Generic HTTP tools.
- User-configurable MCP servers.
- LAN or internet exposure.
