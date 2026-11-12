import httpx
import pytest
import respx

from indico_mcp.client import IndicoClient, IndicoError
from indico_mcp.config import InstanceConfig

BASE = "https://indico.example"


@pytest.fixture
async def client():
    c = IndicoClient(InstanceConfig(name="default", base_url=BASE, token="secret"))
    yield c
    await c.aclose()


@pytest.fixture
async def anon_client():
    c = IndicoClient(InstanceConfig(name="default", base_url=BASE, token=None))
    yield c
    await c.aclose()


# --- export -----------------------------------------------------------------

@respx.mock
async def test_export_sends_token_and_drops_none_params(client):
    route = respx.get(f"{BASE}/export/event/1.json").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    assert await client.export("event/1", detail="contributions", limit=None) == {"results": []}
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer secret"
    assert dict(request.url.params) == {"detail": "contributions"}


@respx.mock
async def test_no_token_means_no_authorization_header(anon_client):
    route = respx.get(f"{BASE}/export/event/1.json").mock(
        return_value=httpx.Response(200, json={})
    )
    await anon_client.export("event/1")
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_export_http_errors_keep_status_code(client, status):
    respx.get(f"{BASE}/export/event/1.json").mock(return_value=httpx.Response(status))
    with pytest.raises(IndicoError) as exc:
        await client.export("event/1")
    assert exc.value.status_code == status


@respx.mock
async def test_export_html_login_page_is_an_error(anon_client):
    respx.get(f"{BASE}/export/event/1.json").mock(
        return_value=httpx.Response(200, text="<html>login</html>",
                                    headers={"content-type": "text/html"})
    )
    with pytest.raises(IndicoError, match="No token is configured"):
        await anon_client.export("event/1")


@respx.mock
async def test_network_error_becomes_indico_error(client):
    respx.get(f"{BASE}/export/event/1.json").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(IndicoError, match="Network error"):
        await client.export("event/1")


@respx.mock
async def test_api_404_status_code(client):
    # search_events_by_keyword relies on this to fall back to the legacy endpoint.
    respx.get(f"{BASE}/api/search/").mock(return_value=httpx.Response(404))
    with pytest.raises(IndicoError) as exc:
        await client.api("search/", q="x")
    assert exc.value.status_code == 404


# --- download ---------------------------------------------------------------

@respx.mock
async def test_download_relative_url_and_content_disposition(client):
    route = respx.get(f"{BASE}/event/1/attachments/2/3/talk.pdf").mock(
        return_value=httpx.Response(
            200, content=b"%PDF",
            headers={"content-type": "application/pdf",
                     "content-disposition": 'inline; filename="slides.pdf"'},
        )
    )
    result = await client.download("/event/1/attachments/2/3/talk.pdf")
    assert route.called
    assert result.filename == "slides.pdf"
    assert result.content == b"%PDF"
    assert result.size == 4
    assert result.content_type == "application/pdf"


@respx.mock
async def test_download_filename_falls_back_to_url(client):
    respx.get(f"{BASE}/files/report.pdf").mock(return_value=httpx.Response(200, content=b"x"))
    result = await client.download(f"{BASE}/files/report.pdf?download=1")
    assert result.filename == "report.pdf"


@respx.mock
@pytest.mark.parametrize("header_name, expected", [
    ("../../etc/passwd", "passwd"),
    ("/tmp/evil.sh", "evil.sh"),
    ("..", "download"),
])
async def test_download_filename_is_reduced_to_basename(client, header_name, expected):
    respx.get(f"{BASE}/f").mock(return_value=httpx.Response(
        200, content=b"x", headers={"content-disposition": f'attachment; filename="{header_name}"'},
    ))
    assert (await client.download(f"{BASE}/f")).filename == expected


@respx.mock
@pytest.mark.parametrize("url", [
    "https://evil.example/f",
    "https://indico.example.evil.example/f",
    "http://indico.example:8080/f",
])
async def test_download_rejects_other_hosts_without_requesting(client, url):
    with pytest.raises(IndicoError, match="does not match"):
        await client.download(url)
    assert not respx.calls  # the token was never sent anywhere


@respx.mock
async def test_download_non_http_scheme_stays_on_configured_host(client):
    # Anything not starting with http(s):// is treated as a path relative to the
    # configured instance, so it can never reach a local file or another host.
    route = respx.route(host="indico.example").mock(return_value=httpx.Response(404))
    with pytest.raises(IndicoError) as exc:
        await client.download("file:///etc/passwd")
    assert exc.value.status_code == 404
    assert route.called
    assert len(respx.calls) == 1


@respx.mock
async def test_download_rejects_cross_host_redirect(client):
    respx.get(f"{BASE}/f").mock(return_value=httpx.Response(
        302, headers={"location": "https://evil.example/f"},
    ))
    evil = respx.get("https://evil.example/f").mock(return_value=httpx.Response(200, content=b"x"))
    with pytest.raises(IndicoError, match="redirected"):
        await client.download(f"{BASE}/f")
    assert "Authorization" not in evil.calls.last.request.headers


@respx.mock
async def test_download_size_limit(client):
    respx.get(f"{BASE}/f").mock(return_value=httpx.Response(200, content=b"x" * 11))
    with pytest.raises(IndicoError, match="size limit"):
        await client.download(f"{BASE}/f", max_size=10)


# --- OAuth errors from the export API ----------------------------------------

def _oauth_error_page(error: str, description: str) -> httpx.Response:
    # Indico's export API answers a token it refuses with HTTP 400 and its
    # generic HTML error page, not 401 with JSON.
    html = f"<html><body><p>OAuth error: {error}: {description}</p></body></html>"
    return httpx.Response(400, text=html, headers={"content-type": "text/html; charset=utf-8"})


@respx.mock
async def test_export_reports_an_invalid_token(client):
    respx.get(f"{BASE}/export/event/1.json").mock(return_value=_oauth_error_page(
        "invalid_token",
        "The access token provided is expired, revoked, malformed, or invalid for other reasons.",
    ))
    with pytest.raises(IndicoError, match="invalid_token") as exc:
        await client.export("event/1")
    assert exc.value.status_code == 400
    assert "HTML instead of JSON" not in str(exc.value)


@respx.mock
async def test_export_names_the_missing_scope(client):
    respx.get(f"{BASE}/export/event/1.json").mock(return_value=_oauth_error_page(
        "insufficient_scope",
        "The request requires higher privileges than provided by the access token.",
    ))
    with pytest.raises(IndicoError, match="read:legacy_api"):
        await client.export("event/1")
