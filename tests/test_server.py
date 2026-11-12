import re

import pytest
from fastmcp import Client

from indico_mcp import server


class FakeClient:
    """Stands in for IndicoClient; returns a canned export response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def export(self, resource, **params):
        self.calls.append((resource, params))
        return self.response


def use_fake(monkeypatch, response):
    fake = FakeClient(response)
    monkeypatch.setattr(server, "_client", lambda instance: fake)
    return fake


async def test_server_starts_and_registers_documented_tools(monkeypatch):
    monkeypatch.setenv("INDICO_BASE_URL", "https://indico.example")
    async with Client(server.app) as c:
        registered = {t.name for t in await c.list_tools()}
        instances = await c.call_tool("list_instances", {})
    documented = set(re.findall(r"^  (\w+)\s+—", server.__doc__, re.MULTILINE))
    # stdio is the default transport; HTTP-only tools stay hidden here.
    assert registered == documented - {"get_attachment_content"}
    assert instances.data == {"default": "default", "instances": ["default"]}


async def test_get_event_contributions_pagination(monkeypatch):
    contribs = [{"id": i, "title": f"Talk {i}"} for i in range(5)]
    use_fake(monkeypatch, {"results": [{"id": "1", "contributions": contribs}]})

    page = await server.get_event_contributions(1, limit=2, offset=2)
    assert [c["id"] for c in page["items"]] == [2, 3]
    assert page["pagination"] == {
        "total": 5, "returned": 2, "limit": 2, "offset": 2,
        "has_more": True, "next_offset": 4,
    }

    last = await server.get_event_contributions(1, limit=2, offset=4)
    assert last["pagination"]["has_more"] is False
    assert last["pagination"]["next_offset"] is None


async def test_get_event_details_not_found(monkeypatch):
    use_fake(monkeypatch, {"results": []})
    with pytest.raises(ValueError, match="not found"):
        await server.get_event_details(1)


async def test_get_category_contributions_annotates_event(monkeypatch):
    fake = use_fake(monkeypatch, {"count": 1, "results": [
        {"id": "7", "title": "Seminar", "startDate": "2026-01-02",
         "contributions": [{"id": 1, "title": "Talk"}]},
    ]})
    result = await server.get_category_contributions(5, limit=9999)
    assert result == [{"id": 1, "title": "Talk", "event_id": "7",
                       "event_title": "Seminar", "event_start": "2026-01-02"}]
    resource, params = fake.calls[0]
    assert resource == "categ/5"
    assert params["detail"] == "contributions"
    assert params["limit"] == 500


async def test_get_category_contributions_explains_missing_scope(monkeypatch):
    use_fake(monkeypatch, {"count": 3, "results": [{"id": "7", "contributions": []}]})
    with pytest.raises(ValueError, match="legacy_api"):
        await server.get_category_contributions(5)


async def test_read_only_hides_book_room(monkeypatch):
    monkeypatch.setenv("INDICO_BASE_URL", "https://indico.example")
    server.set_read_only(True)
    try:
        async with Client(server.app) as c:
            names = {t.name for t in await c.list_tools()}
    finally:
        server.set_read_only(False)
    assert "book_room" not in names
    assert "get_event_details" in names
    async with Client(server.app) as c:
        assert "book_room" in {t.name for t in await c.list_tools()}
