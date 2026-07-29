# AI Workbench Web

The local Next.js interface for AI Workbench. It presents the three-column developer workspace and calls the loopback FastAPI service directly through the generated OpenAPI contract.

## Development

From the repository root:

```bash
pnpm dev:web
```

The default API origin is `http://127.0.0.1:8000`. Override it with `NEXT_PUBLIC_API_BASE_URL` in an uncommitted local environment file when necessary.

## Verification

```bash
pnpm --filter web lint
```

```bash
pnpm --filter web typecheck
```

```bash
pnpm --filter web test
```

```bash
pnpm --filter web build
```

```bash
pnpm --filter web test:e2e
```

The browser tests use mocked API responses for UI lifecycle coverage. Repository path, authorization, scanning, and persistence security are verified by the Python test suite and PostgreSQL CI job.

## Contract

FastAPI OpenAPI is the sole contract source. Regenerate the committed JSON and TypeScript declarations from the repository root:

```bash
pnpm contract:generate
```

Do not hand-edit `packages/api-contract/openapi.json` or `packages/api-contract/schema.d.ts`.
