"""
Per-caller authentication for the streamable-HTTP transport.

In HTTP mode every MCP request must carry the caller's own Indico token as
``Authorization: Bearer <token>``. ``IndicoTokenVerifier`` checks that token
against the Indico instance before any tool runs, so a revoked or invalid token
is answered with HTTP 401 at the transport rather than surfacing later as a
tool error. A gateway or MCP client can only notice a dead credential (and ask
the user to re-authorise) when it sees that 401.

The check is ``GET /api/user/``, which accepts the ``read:user`` or
``read:everything`` scope. Indico answers 401 (invalid token) or 403
(insufficient scope) for a token it refuses, and ``200 null`` when the request
carries no usable identity.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .client import IndicoError

log = logging.getLogger(__name__)


class IndicoUnavailableError(IndicoError):
    """Indico could not answer a token check (network error or 5xx)."""


class _IndicoUnavailableMiddleware:
    """Answer 503 when the token check could not reach Indico.

    Sits outside the authentication middleware. Without it the verifier's
    exception would escape as a bare 500 with a traceback in the server log.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except IndicoUnavailableError as exc:
            log.warning("Token check failed, Indico unavailable: %s", exc)
            response = JSONResponse(
                {"error": "temporarily_unavailable", "error_description": str(exc)},
                status_code=503,
            )
            await response(scope, receive, send)


@dataclass
class _CacheEntry:
    access: AccessToken
    expires: float


class IndicoTokenVerifier(TokenVerifier):
    """Resolve a bearer token to an Indico user via ``GET /api/user/``.

    Successful lookups are cached for ``cache_ttl_seconds``, keyed by the
    token's SHA-256 so the raw token is never a dict key. Rejections are not
    cached, so a token the user just re-authorised works immediately.

    Only a definite refusal from Indico (401/403, or ``null`` user) rejects
    the token. A network error or 5xx raises ``IndicoUnavailableError``
    instead, which the transport answers with 503: an Indico outage must not
    look like a revoked token, or clients would throw away credentials that are
    still valid.
    """

    def __init__(
        self,
        base_url: str,
        *,
        cache_ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._user_url = f"{base_url.rstrip('/')}/api/user/"
        self._ttl = cache_ttl_seconds
        self._clock = clock
        self._cache: dict[str, _CacheEntry] = {}

    def get_middleware(self) -> list:
        return [Middleware(_IndicoUnavailableMiddleware), *super().get_middleware()]

    def _prune(self, now: float) -> None:
        for key in [k for k, e in self._cache.items() if e.expires <= now]:
            del self._cache[key]

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        self._prune(now)
        if (entry := self._cache.get(key)) is not None:
            return entry.access

        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(
                    self._user_url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
        except httpx.RequestError as exc:
            raise IndicoUnavailableError(
                f"Network error verifying token at {self._user_url}: {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            return None
        if not resp.is_success:
            raise IndicoUnavailableError(
                f"Indico returned HTTP {resp.status_code} verifying a token", resp.status_code
            )

        user = resp.json()
        if not isinstance(user, dict) or user.get("id") is None:
            return None

        access = AccessToken(
            token=token,
            client_id="indico",
            scopes=[],
            subject=str(user["id"]),
            claims={"email": user.get("email")},
        )
        self._cache[key] = _CacheEntry(access=access, expires=now + self._ttl)
        return access
