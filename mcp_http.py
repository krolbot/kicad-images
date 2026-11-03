"""Authenticated Streamable HTTP transport for the KiCad MCP toolkit."""
from __future__ import annotations

import argparse
import os
import secrets
from collections.abc import Awaitable, Callable

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Message, Receive, Scope, Send

TOKEN_ENV = "KICAD_MCP_BEARER_TOKEN"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_ALLOWED_HOSTS = (
    "kicad-mcp.nlab.ltd",
    "kicad-mcp.nlab.ltd:*",
    "kicad-mcp-http",
    "kicad-mcp-http:*",
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
)
DEFAULT_ALLOWED_ORIGINS = ("https://kicad-mcp.nlab.ltd",)

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _csv(value: str | None, default: tuple[str, ...]) -> list[str]:
    if value is None:
        return list(default)
    parsed = [item.strip() for item in value.split(",") if item.strip()]
    if not parsed:
        raise ValueError("An allowlist environment variable cannot be empty")
    return parsed


class BearerAuthMiddleware:
    """Reject every HTTP request unless it carries the configured bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError(f"{TOKEN_ENV} must not be empty")
        self.app = app
        self.token = token

    @staticmethod
    def _presented_token(scope: Scope) -> str | None:
        values = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
        if len(values) != 1:
            return None
        try:
            scheme, token = values[0].decode("latin-1").split(" ", 1)
        except ValueError:
            return None
        if scheme.lower() != "bearer" or not token:
            return None
        return token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            presented = self._presented_token(scope)
            if presented is None or not secrets.compare_digest(presented, self.token):
                body = b'{"error":"unauthorized"}'
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", b"Bearer"),
                        (b"cache-control", b"no-store"),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_app(token: str) -> ASGIApp:
    from toolkit_mcp import mcp

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv(os.getenv("KICAD_MCP_ALLOWED_HOSTS"), DEFAULT_ALLOWED_HOSTS),
        allowed_origins=_csv(os.getenv("KICAD_MCP_ALLOWED_ORIGINS"), DEFAULT_ALLOWED_ORIGINS),
    )
    app_options = {
        "streamable_http_path": "/mcp",
        "json_response": False,
        "stateless_http": False,
        "max_request_body_size": 4 * 1024 * 1024,
        "session_idle_timeout": 900,
        "max_sessions": 32,
        "transport_security": security,
        "host": os.getenv("KICAD_MCP_HOST", DEFAULT_HOST),
    }
    app = mcp.streamable_http_app(**app_options)
    return BearerAuthMiddleware(app, token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("KICAD_MCP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("KICAD_MCP_PORT", str(DEFAULT_PORT))))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    token = os.getenv(TOKEN_ENV)
    if not token:
        parser.error(f"{TOKEN_ENV} is required")

    import uvicorn

    uvicorn.run(build_app(token), host=args.host, port=args.port, log_level="info", access_log=True)


if __name__ == "__main__":
    main()
