$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    pnpm check:web
    uv run --project apps/api ruff check apps/api db/migrations
    uv run --project apps/api mypy apps/api/src apps/api/tests db/migrations/env.py
    uv run --project apps/api pytest apps/api/tests
}
finally {
    Pop-Location
}
