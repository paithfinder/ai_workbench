# Contributing

The project is being built in vertical slices. Each change should leave its slice runnable, tested, documented, and secure by default.

## Before opening a change

1. Read the architecture overview and relevant ADRs.
2. Do not expand filesystem, network, model, or execution permissions without updating the threat model.
3. Add migrations rather than editing an already-released schema in place.
4. Keep LangGraph orchestration thin: authorization, retrieval, citation validation, and memory policy belong in reusable application services.
5. Add tests for failure and cancellation paths, not only happy paths.

## Quality checks

```bash
pnpm check
```

A change is not complete if lint, type checks, tests, migration replay, or production builds fail.

## Commits

Use concise imperative commit messages. Do not commit generated local data, `.env` files, credentials, logs, test reports, or private fixtures.
