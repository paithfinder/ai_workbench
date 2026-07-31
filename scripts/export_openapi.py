from __future__ import annotations

import json
from pathlib import Path

from knowledge_workbench.config import Settings
from knowledge_workbench.main import create_app

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "packages" / "api-contract" / "openapi.json"


def main() -> None:
    app = create_app(Settings(app_env="test"))
    schema = app.openapi()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Exported OpenAPI schema to {TARGET}")


if __name__ == "__main__":
    main()
