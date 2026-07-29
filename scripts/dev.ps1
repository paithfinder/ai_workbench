param(
    [switch]$SkipInfrastructure
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path (Join-Path $root ".env"))) {
    throw "Missing .env. Copy .env.example to .env and replace the local database password."
}

if (-not $SkipInfrastructure) {
    docker compose --env-file (Join-Path $root ".env") -f (Join-Path $root "infra/compose/docker-compose.yml") up -d postgres
}

Write-Host "Infrastructure is ready. Start the applications in separate terminals:"
Write-Host "  uv run --project apps/api uvicorn ai_workbench_api.main:app --app-dir apps/api/src --host 127.0.0.1 --port 8000 --reload"
Write-Host "  pnpm dev:web"
