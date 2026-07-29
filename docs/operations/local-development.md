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
