import pytest

from indico_mcp import server
from indico_mcp.models import normalize_contribution, normalize_event, normalize_session


EVENT_NOTE = {"html": "<p>Event minutes &amp; decisions</p>", "text": "Event minutes & decisions"}
CONTRIBUTION_NOTE = {"html": "<p>Contribution minutes</p>"}
BLOCK_NOTE = {"html": "<p>Block minutes</p>"}


@pytest.mark.parametrize("normalizer", [normalize_event, normalize_contribution, normalize_session])
@pytest.mark.parametrize("note", [None, {}])
def test_absent_or_empty_notes_are_omitted(normalizer, note):
    assert "note" not in normalizer({"id": "1", "note": note})
    assert "note" not in normalizer({"id": "1"})


@pytest.mark.asyncio
async def test_minutes_reach_detailed_tools_but_not_event_listings(monkeypatch):
    contribution = {"id": "2", "title": "Talk", "note": CONTRIBUTION_NOTE}
    event = {"id": "1", "title": "Meeting", "note": EVENT_NOTE,
             "contributions": [contribution],
             "sessions": [{"id": 3, "title": "Block", "note": BLOCK_NOTE,
                           "contributions": [contribution]}]}

    class Client:
        async def export(self, resource, **params):
            return {"results": [event]}

    monkeypatch.setattr(server, "_client", lambda instance: Client())
    details = await server.get_event_details(1)
    assert details["note"] == EVENT_NOTE
    assert details["contributions"][0]["note"] == CONTRIBUTION_NOTE
    contributions = await server.get_event_contributions(1)
    assert contributions["items"][0]["note"] == CONTRIBUTION_NOTE
    sessions = await server.get_event_sessions(1)
    assert sessions[0]["note"] == BLOCK_NOTE
    assert sessions[0]["contributions"][0]["note"] == CONTRIBUTION_NOTE
    assert "note" not in (await server.search_category_events(1))[0]


def test_event_note_opt_in_preserves_export_fields():
    assert normalize_event({"note": EVENT_NOTE}, include_note=True)["note"] == EVENT_NOTE
    assert "note" not in normalize_event({"note": None}, include_note=True)
