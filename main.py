"""ASGI entry point used by Uvicorn and hosting platforms."""

from tgdrive.app import create_app

app = create_app()
