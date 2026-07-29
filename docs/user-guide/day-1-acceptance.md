# Day 1 Acceptance Checklist

## Foundation

- [x] Root workspace installs with `pnpm install --frozen-lockfile`.
- [x] Python workspace installs with `uv sync --project apps/api --extra dev --locked`.
- [x] PostgreSQL Compose configuration binds only to `127.0.0.1` and has no fallback password.
- [ ] Alembic upgrades an empty PostgreSQL database to `head`.
- [ ] Alembic downgrades to `base` and replays to `head` in CI.

## API

- [x] `/api/v1/health` returns application, version, API status, database status, and `model_configured` without exposing secrets or a DSN.
- [x] Missing OpenAI configuration does not prevent startup.
- [x] CORS defaults to local Web origins only.
- [x] Errors carry a request/correlation ID and do not expose stack traces.
- [x] Structured logging redacts representative OpenAI, GitHub, and Notion tokens.

## Web

- [x] The root route renders a desktop three-column workspace.
- [x] Empty source, conversation, evidence, and run states are understandable without setup documentation.
- [x] API offline, degraded, and model-not-configured states are visible without crashing the page.
- [x] Keyboard focus is visible and semantic landmarks are present.
- [x] Narrow viewports degrade safely without horizontal overflow.

## Quality

- [x] Python Ruff, MyPy, and Pytest pass.
- [x] Web ESLint, strict TypeScript, health-contract tests, and production build pass.
- [x] CI is configured to run all of the above with least-privilege workflow permissions and bounded timeouts; the hosted run is pending.
- [x] `.env`, credentials, local data, logs, dependency trees, and build output are ignored.
- [x] README, architecture overview, stack ADR, threat model, operations guide, and security policy match the implementation.

## Environment note

Docker is not available in the original implementation environment. The database-related boxes must remain unchecked until Compose and migration replay have been executed on a Docker-enabled Windows machine or in CI.
