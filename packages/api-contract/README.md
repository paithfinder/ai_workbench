# API contract package

FastAPI's OpenAPI document is the sole contract source of truth. The committed
`openapi.json` and `schema.d.ts` files are generated artifacts; do not edit them
or hand-maintain duplicate request and response DTOs.

Generate both artifacts from the repository root:

```bash
pnpm contract:generate
```

CI can detect drift with:

```bash
pnpm contract:check
```
