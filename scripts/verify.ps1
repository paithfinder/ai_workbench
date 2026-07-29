$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    pnpm check
    pnpm --filter web test:e2e
    uv run --project apps/api alembic -c apps/api/alembic.ini upgrade head --sql | Out-Null
}
finally {
    Pop-Location
}
