# Day 2 Acceptance Checklist

## Authorization lifecycle

- [ ] Pasting an existing absolute Windows directory produces a canonical preview; it does not persist or scan the repository.
- [ ] Authorization requires explicit confirmation and consumes a short-lived token exactly once.
- [ ] Relative, drive-relative, UNC/network, device-namespace, ADS, reserved-device, and remote-drive paths are rejected without returning the submitted path.
- [ ] Reauthorizing a revoked root advances its authorization epoch; stale scan or revoke requests fail closed.
- [ ] Revocation disables future source access and cancels pending index jobs; it clearly does not claim to purge historical manifests or audit records.

## Secure scan

- [ ] A scan is read-only, uses no shell/Git/subprocess, and obeys the configured time, depth, file, directory, single-file, and total-byte limits.
- [ ] Symlinks, junctions, mount points, and other reparse descendants are never followed; Day 2 Windows scans fail closed by denying all descendant directories until handle-bound enumeration is available.
- [ ] `.env*`, credentials, private keys, VCS internals, dependencies, build output, binaries, and oversized files do not enter a manifest.
- [ ] Root `.gitignore` rules apply but cannot negate a hard security exclusion.
- [ ] Manifests contain normalized relative paths and metadata only, have deterministic hashes, and an unchanged non-empty version has at most one pending index job.
- [ ] A concurrent revoke, reauthorize, root replacement, or changed file invalidates or safely skips the in-flight result rather than committing stale access.

## Web

- [ ] The repository list, authorization, scan, indexing, and revocation states come from FastAPI rather than fixtures.
- [ ] Authorization and “include in current context” are separate; only authorized, manifest-ready repositories can be selected.
- [ ] The authorization dialog supports keyboard focus containment, Escape, focus restoration, associated errors, and duplicate-submit prevention.
- [ ] The UI shows no fabricated scan percentage, vector count, evidence, citation, or LangGraph trace.
- [ ] Desktop, tablet, and mobile layouts have no horizontal overflow; canonical paths wrap safely.

## Quality and environment

- [ ] Generated OpenAPI JSON and TypeScript types match FastAPI.
- [ ] Ruff, MyPy, Pytest, ESLint, strict TypeScript, Vitest, Playwright, and production build pass.
- [ ] PostgreSQL upgrades to `head`, integration tests pass, then downgrade to `base` and replay to `head` pass.
- [ ] Windows CI exercises path identity and reparse controls; link-fixture skips are limited to an exact OS privilege error.
- [ ] Logs, API errors, audit payloads, and committed files contain no preview token, canonical path, source body, credential, private key, `.env`, local database, or generated local manifest.

## Environment note

Docker is not available in the original implementation environment. PostgreSQL boxes must remain unchecked until a hosted CI run or Docker-enabled Windows machine executes the real migration and integration sequence. Offline Alembic SQL generation alone is not equivalent evidence.
