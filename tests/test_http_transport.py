"""
Integration tests for the streamable-HTTP transport.

Both sides run as real uvicorn servers on loopback ports: indico-mcp in HTTP
mode, and a small stand-in for an Indico instance that implements just the
endpoints these tests touch (`/api/user/` and one `/export/` resource).
Nothing inside the indico-mcp process is patched.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from fastmcp import Client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from _servers import free_port, serve
from indico_mcp import server
from indico_mcp.config import Config

GOOD = "good-token"
DOWN = "indico-is-down"

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@dataclass
class FakeIndico:
    url: str
    user_lookups: int = 0
    export_auth: list[str | None] = field(default_factory=list)


@pytest.fixture
def fake_indico() -> Iterator[FakeIndico]:
    port = free_port()
    state = FakeIndico(url=f"http://127.0.0.1:{port}")

    async def api_user(request: Request) -> Response:
        state.user_lookups += 1
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {GOOD}":
            return JSONResponse({"id": 7, "email": "u@example.org", "first_name": "U",
                                 "last_name": "Ser", "admin": False})
        if auth == f"Bearer {DOWN}":
            return Response(status_code=503)
        return JSONResponse({"error": "invalid_token"}, status_code=401)

    async def export_event(request: Request) -> Response:
        state.export_auth.append(request.headers.get("authorization"))
        return JSONResponse({"count": 1, "results": [
            {"id": request.path_params["event_id"], "title": "Weekly meeting",
             "startDate": {"date": "2026-01-02", "time": "10:00:00", "tz": "UTC"},
             "contributions": []},
        ]})

    app = Starlette(routes=[
        Route("/api/user/", api_user),
        Route("/export/event/{event_id}.json", export_event),
    ])
    with serve(app, port):
        yield state


@pytest.fixture
def indico_mcp_url(monkeypatch, fake_indico) -> Iterator[str]:
    monkeypatch.setenv("INDICO_BASE_URL", fake_indico.url)
    server.configure_http_auth(Config())
    port = free_port()
    try:
        with serve(server.app.http_app(stateless_http=True), port):
            yield f"http://127.0.0.1:{port}"
    finally:
        server.reset_auth()


async def _post_initialize(url: str, token: str | None) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as http:
        return await http.post(f"{url}/mcp", json=INITIALIZE, headers=headers)


async def test_request_without_bearer_gets_401(indico_mcp_url):
    resp = await _post_initialize(indico_mcp_url, None)
    assert resp.status_code == 401


async def test_bearer_indico_rejects_gets_401(indico_mcp_url, fake_indico):
    resp = await _post_initialize(indico_mcp_url, "revoked-token")
    assert resp.status_code == 401
    assert fake_indico.user_lookups == 1


async def test_indico_outage_is_503_not_401(indico_mcp_url, caplog):
    # A 401 would tell the caller its token is dead; Indico being down is not that.
    with caplog.at_level(logging.WARNING):
        resp = await _post_initialize(indico_mcp_url, DOWN)
    assert resp.status_code == 503
    assert resp.json()["error"] == "temporarily_unavailable"
    assert [(r.name, r.levelname) for r in caplog.records] == [("indico_mcp.auth", "WARNING")]
    assert "HTTP 503" in caplog.records[0].getMessage()


async def test_tool_call_uses_the_callers_own_token(indico_mcp_url, fake_indico):
    async with Client(f"{indico_mcp_url}/mcp", auth=GOOD) as c:
        result = await c.call_tool("get_event_details", {"event_id": 42})
    assert result.data["title"] == "Weekly meeting"
    assert fake_indico.export_auth == [f"Bearer {GOOD}"]


async def test_healthz_needs_no_token(indico_mcp_url):
    async with httpx.AsyncClient() as http:
        resp = await http.get(f"{indico_mcp_url}/healthz")
    assert resp.status_code == 200


# --- configuration -----------------------------------------------------------

def test_http_mode_refuses_a_static_token(monkeypatch):
    # A token in the environment would be served to every caller.
    monkeypatch.setenv("INDICO_BASE_URL", "https://indico.example")
    monkeypatch.setenv("INDICO_TOKEN", "indp_shared")
    with pytest.raises(ValueError, match="INDICO_TOKEN"):
        server.configure_http_auth(Config())
    assert server.app.auth is None


def test_http_mode_refuses_multiple_instances(monkeypatch):
    # A caller's token is only valid at the Indico instance that issued it.
    monkeypatch.setenv("INDICO_INSTANCES", "a,b")
    monkeypatch.setenv("INDICO_A_URL", "https://a.example")
    monkeypatch.setenv("INDICO_B_URL", "https://b.example")
    with pytest.raises(ValueError, match="one Indico instance"):
        server.configure_http_auth(Config())
    assert server.app.auth is None


def test_cli_rejects_bad_http_configuration_with_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("INDICO_BASE_URL", "https://indico.example")
    monkeypatch.setenv("INDICO_TOKEN", "indp_shared")
    with pytest.raises(SystemExit) as exc:
        server.main(["--transport", "http"])
    assert exc.value.code == 2
    assert "INDICO_TOKEN" in capsys.readouterr().err
    assert server.app.auth is None
