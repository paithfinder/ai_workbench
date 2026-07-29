"""Loopback-only development entry point."""

import uvicorn


def main() -> None:
    """Run the API on the documented loopback interface."""
    uvicorn.run(
        "ai_workbench_api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
