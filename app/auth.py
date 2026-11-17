"""Static bearer-token authentication.

Implemented as a plain ASGI middleware wrapped around the whole application, so
it runs before routing, body parsing and any database access, and it covers
every path (including unknown ones).
"""

import hashlib
import hmac

from app.errors import error_body

_UNAUTHORIZED_BODY = error_body("unauthorized", "missing or invalid bearer token")


class BearerAuthMiddleware:
    def __init__(self, app, token: str) -> None:
        self.app = app
        # Only a digest of the token is kept. Comparing fixed-length digests
        # also means the comparison time cannot reveal the token's length.
        self._expected = hashlib.sha256(token.encode()).digest()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            # "lifespan" (startup/shutdown) events are not requests.
            await self.app(scope, receive, send)
            return

        values = [value for name, value in scope["headers"] if name == b"authorization"]
        header = values[0] if len(values) == 1 else None

        if not self._is_authorized(header):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return

        await self.app(scope, receive, send)

    def _is_authorized(self, header: bytes | None) -> bool:
        if header is None:
            return False
        scheme, separator, token = header.partition(b" ")
        # The scheme name is case-insensitive (RFC 7235); the token is not.
        if not separator or scheme.lower() != b"bearer":
            return False
        provided = hashlib.sha256(token).digest()
        return hmac.compare_digest(provided, self._expected)  # constant time
