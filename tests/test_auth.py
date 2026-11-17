import httpx
import pytest
import respx

from indico_mcp.auth import IndicoTokenVerifier, IndicoUnavailableError
from indico_mcp.client import IndicoError

BASE = "https://indico.example"
USER_URL = f"{BASE}/api/user/"
USER = {"id": 42, "email": "a@example.org", "first_name": "A", "last_name": "B", "admin": False}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def verifier(clock):
    return IndicoTokenVerifier(BASE, cache_ttl_seconds=60, clock=clock)


@respx.mock
async def test_valid_token_resolves_to_the_indico_user(verifier):
    route = respx.get(USER_URL).mock(return_value=httpx.Response(200, json=USER))
    access = await verifier.verify_token("tok")
    assert access is not None
    assert access.token == "tok"
    assert access.subject == "42"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_token_indico_refuses_is_rejected(verifier, status):
    respx.get(USER_URL).mock(return_value=httpx.Response(status, json={"error": "invalid_token"}))
    assert await verifier.verify_token("tok") is None


@respx.mock
async def test_null_user_is_rejected(verifier):
    # /api/user/ answers 200 null when the request carries no usable identity.
    respx.get(USER_URL).mock(return_value=httpx.Response(
        200, content=b"null\n", headers={"content-type": "application/json"}))
    assert await verifier.verify_token("tok") is None


@respx.mock
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_indico_server_error_raises_instead_of_rejecting(verifier, status):
    # Answering 401 here would tell the caller the token is bad when Indico is
    # merely down; a gateway would then discard a perfectly good credential.
    respx.get(USER_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(IndicoUnavailableError):
        await verifier.verify_token("tok")


@respx.mock
async def test_network_error_raises_instead_of_rejecting(verifier):
    respx.get(USER_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(IndicoUnavailableError):
        await verifier.verify_token("tok")


@respx.mock
async def test_successful_lookup_is_cached_until_ttl(verifier, clock):
    route = respx.get(USER_URL).mock(return_value=httpx.Response(200, json=USER))
    await verifier.verify_token("tok")
    clock.now += 59
    assert await verifier.verify_token("tok") is not None
    assert route.call_count == 1
    clock.now += 2
    await verifier.verify_token("tok")
    assert route.call_count == 2


@respx.mock
async def test_rejection_is_not_cached(verifier):
    route = respx.get(USER_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=USER)]
    )
    assert await verifier.verify_token("tok") is None
    assert await verifier.verify_token("tok") is not None
    assert route.call_count == 2


@respx.mock
async def test_cache_is_per_token(verifier):
    route = respx.get(USER_URL).mock(return_value=httpx.Response(200, json=USER))
    await verifier.verify_token("tok-a")
    await verifier.verify_token("tok-b")
    assert route.call_count == 2
