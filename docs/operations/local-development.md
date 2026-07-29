# Operations boundary

## Local ports

- Web: `127.0.0.1:3000`
- API: `127.0.0.1:8000`
- PostgreSQL: `127.0.0.1:5432`

Do not change these bindings to `0.0.0.0` without a new threat model, authentication design, TLS termination, CSRF/CORS review, and explicit user approval.

## Configuration

Copy `.env.example` to `.env` and replace the local PostgreSQL password. Provider keys are optional during foundation development. Real provider keys should be stored in Windows Credential Manager once connector support is implemented; environment variables are only a local-development fallback.

## Database lifecycle

- Alembic migrations are the only supported schema creation mechanism.
- The API must not call `create_all()` at startup.
- Every migration must replay from an empty PostgreSQL database in CI.
- Production-like startup must apply `alembic upgrade head` before starting the API.

## Logs

Logs must be structured and pass through the shared redaction layer. Never log database URLs, authorization headers, prompts, retrieved source bodies, or provider responses in full.

## Local repository authorization

The browser cannot grant FastAPI trustworthy access to a local Windows directory.
Paste the absolute directory path in the repository dialog, inspect the canonical
path returned by the API, then explicitly confirm it. Authorization is per root and
revocable; it does not authorize the parent directory or the rest of a drive.

Repository scans are synchronous and bounded. Their defaults are configured with the
`REPOSITORY_SCAN_*` variables in `.env.example`. Raising those limits increases local
resource exposure and should be reviewed rather than done to bypass a scan failure.
The scanner writes no files to the repository and does not run Git or subprocesses.

Revocation blocks future scanner/indexer/retriever access and cancels pending jobs. It
does not purge historical manifests or audit records; purge will be a separate,
explicit destructive operation in a later phase.

## Contract generation

FastAPI OpenAPI is the contract source of truth. After changing endpoint schemas, run:

```bash
pnpm contract:generate
```

Commit both generated files under `packages/api-contract`. `pnpm contract:check`
regenerates them and fails when the working tree differs.
