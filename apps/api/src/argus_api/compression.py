"""Gzip for API and dashboard responses (dashboard-performance spec)."""

from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


class Compression:
    """`GZipMiddleware` minus what must stay byte-exact: ranged reads, whose
    partial body cannot be resumed once compressed, and opaque binary files."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip = GZipMiddleware(
            app,
            minimum_size=1024,
            compresslevel=5,
            exclude_content_types=(
                *DEFAULT_EXCLUDED_CONTENT_TYPES,
                "application/octet-stream",
            ),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        ranged = scope["type"] == "http" and any(
            k == b"range" for k, _ in scope["headers"]
        )
        await (self.app if ranged else self.gzip)(scope, receive, send)
