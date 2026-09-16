"""Entry point for `uv run llm-gateway`."""

import uvicorn

from llm_gateway.app import create_app
from llm_gateway.config import HOST, get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(create_app(settings), host=HOST, port=settings.port)


if __name__ == "__main__":
    main()
