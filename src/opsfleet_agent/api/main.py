import uvicorn

from opsfleet_agent.api.app import create_app
from opsfleet_agent.core.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings), host=settings.api_host, port=settings.api_port, proxy_headers=True
    )
