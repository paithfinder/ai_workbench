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

## Day 2 repository-ingestion controls

- A pasted absolute Windows path is canonicalized by FastAPI, then shown back with a
  short-lived, one-use opaque preview token before explicit authorization.
- Relative, drive-relative, UNC/network, device-namespace, alternate-data-stream,
  reserved-device, and remote-drive paths fail closed.
- Authorization persists per canonical root with a root identity and monotonically
  increasing authorization epoch. Revocation disables the source and cancels pending
  jobs without silently purging historical manifests or audit records.
- Scans are read-only, iterative, time/size/count/depth bounded, and never call Git,
  a shell, or a subprocess. Symlinks, junctions, mount points, and other reparse points
  are not followed. POSIX descendants are enumerated through verified no-follow
  directory handles. Python cannot enumerate from a verified directory handle on
  Windows, so the Day 2 Windows policy deliberately scans root files only and records
  descendant directories as denied rather than accepting a check/open race.
- Hard deny rules exclude VCS internals, dependency/build trees, `.env*`, credential
  and private-key material, binary formats, and oversized content. Root `.gitignore`
  rules are convenience filters and cannot override the hard deny layer.
- Immutable manifests contain only normalized relative paths, content hashes, sizes,
  timestamps, and file identity. They never contain source bodies or absolute child
  paths.
- Root and file identities are rechecked around filesystem work, and authorization
  epoch is rechecked under a database row lock before persisting. Concurrent revoke
  or reauthorize operations invalidate an in-flight scan.
- Logs, errors, and audit payloads exclude canonical paths, preview tokens, request
  bodies, source bodies, raw filesystem exceptions, and private-key blocks.

### Residual local-filesystem boundary

These controls reduce TOCTOU exposure on a trusted single-user Windows workstation;
they are not a native sandbox against a malicious local process that can continuously
replace files or filesystem objects during a scan. Repository access must not be
exposed over LAN or internet without a new authorization and sandbox design.

## Required before URL ingestion

- Scheme allowlist, DNS resolution, IP classification, redirect-by-redirect validation, response-size and MIME limits, and timeouts.

## Explicitly unavailable in the MVP

- Shell and subprocess tools.
- Code execution.
- Git writes.
- Generic HTTP tools.
- User-configurable MCP servers.
- LAN or internet exposure.
