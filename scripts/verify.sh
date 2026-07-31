#!/usr/bin/env bash
set -euo pipefail

uv run --package knowledge-workbench-api ruff check apps/api
uv run --package knowledge-workbench-api mypy apps/api/src
uv run --package knowledge-workbench-api pytest apps/api/tests/unit
pnpm contract:generate
pnpm verify
python evals/validate_seeds.py
