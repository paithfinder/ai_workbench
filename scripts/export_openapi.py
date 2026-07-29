"""Export the FastAPI OpenAPI document deterministically."""

from __future__ import annotations

import json
from pathlib import Path

from ai_workbench_api.main import create_app

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "packages" / "api-contract" / "openapi.json"


def main() -> None:
    """Write the API contract without starting application lifespan services."""
    document = create_app(check_database_on_startup=False).openapi()
    OUTPUT_PATH.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
