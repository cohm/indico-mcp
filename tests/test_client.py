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


@respx.mock
@pytest.mark.parametrize('base_path', ['', '/indico'])
@pytest.mark.parametrize('scope', [
    '', 'contributions/20/', 'contributions/20/subcontributions/30/', 'sessions/40/',
])
async def test_download_retries_protected_attachment(base_path, scope):
    base = BASE + base_path
    c = IndicoClient(InstanceConfig('test', base, 'secret'))
    display = respx.get(f'{base}/event/1/{scope}attachments/2/3/talk.pdf').mock(
        return_value=httpx.Response(403)
    )
    export = respx.get(f'{base}/export/event/1/material/2/3.bin').mock(
        return_value=httpx.Response(200, content=b'%PDF', headers={'content-type': 'application/pdf'})
    )
    try:
        result = await c.download(f'event/1/{scope}attachments/2/3/talk.pdf?download=1')
        assert (result.content, result.filename, result.content_type, result.size) == (
            b'%PDF', 'talk.pdf', 'application/pdf', 4,
        )
        assert display.call_count == export.call_count == 1
        assert export.calls.last.request.headers['Authorization'] == 'Bearer secret'
        assert not export.calls.last.request.url.query
    finally:
        await c.aclose()


@respx.mock
@pytest.mark.parametrize('path,status', [
    ('/event/1/attachments/2/3/talk.pdf', 200),
    ('/event/1/attachments/2/3/talk.pdf', 401),
    ('/event/1/attachments/2/3/talk.pdf', 404),
    ('/event/1/attachments/2/3/talk.pdf', 500),
    ('/other/talk.pdf', 403),
    ('/event/1/attachments/2/3/talk.pdf/extra', 403),
    ('/event/1/attachments/2/3/', 403),
    ('/indico/event/1/attachments/2/3/talk.pdf', 403),
])
async def test_download_does_not_retry_other_paths_or_statuses(client, path, status):
    route = respx.get(BASE + path).mock(return_value=httpx.Response(status, content=b'x'))
    if status == 200:
        assert (await client.download(path)).content == b'x'
    else:
        with pytest.raises(IndicoError) as exc:
            await client.download(path)
        assert exc.value.status_code == status
    assert route.call_count == len(respx.calls) == 1


@respx.mock
async def test_download_anonymous_denial_is_not_retried(anon_client):
    respx.get(f'{BASE}/event/1/attachments/2/3/talk.pdf').mock(return_value=httpx.Response(403))
    with pytest.raises(IndicoError) as exc:
        await anon_client.download('/event/1/attachments/2/3/talk.pdf')
    assert exc.value.status_code == 403
    assert len(respx.calls) == 1


@respx.mock
async def test_download_retry_uses_request_auth_not_client_defaults(anon_client):
    # Mirrors #9's per-request auth without introducing its transport API here.
    async def add_auth(request):
        if '/export/' not in request.url.path:
            request.headers['Authorization'] = 'Bearer caller-token'
    anon_client._http.event_hooks['request'].append(add_auth)
    respx.get(f'{BASE}/event/1/attachments/2/3/talk.pdf').mock(return_value=httpx.Response(403))
    export = respx.get(f'{BASE}/export/event/1/material/2/3.bin').mock(
        return_value=httpx.Response(200, content=b'x')
    )
    await anon_client.download('/event/1/attachments/2/3/talk.pdf')
    assert 'Authorization' not in anon_client._http.headers
    assert export.calls.last.request.headers['Authorization'] == 'Bearer caller-token'


@respx.mock
@pytest.mark.parametrize('status', [400, 401, 403, 404, 500])
async def test_download_export_error_is_preserved_without_further_retry(client, status):
    respx.get(f'{BASE}/event/1/attachments/2/3/talk.pdf').mock(return_value=httpx.Response(403))
    export = respx.get(f'{BASE}/export/event/1/material/2/3.bin').mock(return_value=httpx.Response(status))
    with pytest.raises(IndicoError) as exc:
        await client.download('/event/1/attachments/2/3/talk.pdf')
    assert exc.value.status_code == status
    assert export.call_count == 1
    assert len(respx.calls) == 2


@respx.mock
@pytest.mark.parametrize('redirect_export', [False, True])
async def test_download_retry_rejects_cross_host_redirect(client, redirect_export):
    redirect = httpx.Response(302, headers={'location': 'https://evil.example/f'})
    respx.get(f'{BASE}/event/1/attachments/2/3/talk.pdf').mock(
        return_value=httpx.Response(403) if redirect_export else redirect
    )
    if redirect_export:
        respx.get(f'{BASE}/export/event/1/material/2/3.bin').mock(return_value=redirect)
    evil = respx.get('https://evil.example/f').mock(return_value=httpx.Response(403))
    with pytest.raises(IndicoError, match='unexpected host'):
        await client.download('/event/1/attachments/2/3/talk.pdf')
    assert 'Authorization' not in evil.calls.last.request.headers
    assert len(respx.calls) == (3 if redirect_export else 2)


@respx.mock
async def test_download_retry_keeps_size_limit_and_network_errors(client):
    respx.get(f'{BASE}/event/1/attachments/2/3/talk.pdf').mock(return_value=httpx.Response(403))
    export = respx.get(f'{BASE}/export/event/1/material/2/3.bin').mock(
        return_value=httpx.Response(200, content=b'x' * 11)
    )
    with pytest.raises(IndicoError, match='size limit'):
        await client.download('/event/1/attachments/2/3/talk.pdf', max_size=10)
    export.mock(side_effect=httpx.ConnectError('boom'))
    with pytest.raises(IndicoError, match='Network error downloading'):
        await client.download('/event/1/attachments/2/3/talk.pdf')
