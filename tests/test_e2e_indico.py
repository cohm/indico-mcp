"""
End-to-end tests of HTTP mode against a real Indico instance.

Opt-in, because they need network access and (for the full flow) a personal
token:

    INDICO_E2E_URL=https://indico.cern.ch \\
    INDICO_E2E_TOKEN=indp_... \\
    uv run pytest tests/test_e2e_indico.py -v

The token needs the "Classic API (read only)" scope (`read:legacy_api`, for
`/export/`) and "User information (read only)" (`read:user`, for the token
check) or "Everything (only GET)". Without INDICO_E2E_TOKEN only the
rejection test runs.
"""

import os

import httpx
import pytest
from fastmcp import Client

from _servers import free_port, serve
from indico_mcp import server
from indico_mcp.auth import IndicoTokenVerifier
from indico_mcp.config import Config

# Read at import time: the autouse conftest fixture clears INDICO_* per test.
E2E_URL = os.environ.get("INDICO_E2E_URL")
E2E_TOKEN = os.environ.get("INDICO_E2E_TOKEN")

pytestmark = pytest.mark.skipif(not E2E_URL, reason="set INDICO_E2E_URL to run against a real Indico")

needs_token = pytest.mark.skipif(not E2E_TOKEN, reason="set INDICO_E2E_TOKEN for the authenticated flow")


@pytest.fixture
def indico_mcp_url(monkeypatch):
    monkeypatch.setenv("INDICO_BASE_URL", E2E_URL)
    server.configure_http_auth(Config())
    port = free_port()
    try:
        with serve(server.app.http_app(stateless_http=True), port):
            yield f"http://127.0.0.1:{port}"
    finally:
        server.reset_auth()


async def test_real_indico_rejects_an_unknown_token():
    verifier = IndicoTokenVerifier(E2E_URL)
    assert await verifier.verify_token("indp_this-token-does-not-exist") is None


async def test_unknown_token_gets_401_from_indico_mcp(indico_mcp_url):
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"{indico_mcp_url}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "e2e", "version": "0"}}},
            headers={"Accept": "application/json, text/event-stream",
                     "Authorization": "Bearer indp_this-token-does-not-exist"},
        )
    assert resp.status_code == 401


@needs_token
async def test_real_token_is_verified_and_reaches_the_export_api(indico_mcp_url):
    async with Client(f"{indico_mcp_url}/mcp", auth=E2E_TOKEN) as c:
        result = await c.call_tool("browse_category", {"category_id": 0})
    assert not result.is_error
