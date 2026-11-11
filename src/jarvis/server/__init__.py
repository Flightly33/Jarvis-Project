"""The local web interface: a small HTTP + WebSocket server bound to this machine only."""

from jarvis.server.app import create_app

__all__ = ["create_app"]
