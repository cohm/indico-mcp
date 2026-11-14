import pytest

from indico_mcp import server
from indico_mcp.models import normalize_contribution, normalize_event, normalize_session


EVENT_NOTE = {
    "html": "<p>Event minutes &amp; decisions</p>",
    "url": "https://indico.example/event/1/note/",
    "modified_dt": "2026-11-01T12:00:00+00:00",
    "user": 7,
}
CONTRIBUTION_NOTE = {"html": "<p>Contribution minutes</p>"}
BLOCK_NOTE = {"html": "<p>Block minutes</p>"}


@pytest.mark.parametrize("normalizer", [normalize_event, normalize_contribution, normalize_session])
@pytest.mark.parametrize("note", [None, {}])
def test_absent_or_empty_notes_are_omitted(normalizer, note):
    assert "note" not in normalizer({"id": "1", "note": note}, include_note=True)
    assert "note" not in normalizer({"id": "1"}, include_note=True)


@pytest.fixture
def raw_event(monkeypatch):
    contribution = {
        "id": "2", "title": "Talk", "note": CONTRIBUTION_NOTE,
        "folders": [{"title": "Slides", "attachments": [{"id": 5, "type": "file"}]}],
    }
    event = {"id": "1", "title": "Meeting", "note": EVENT_NOTE,
             "contributions": [contribution],
             "sessions": [{"id": 3, "title": "Block", "note": BLOCK_NOTE,
                           "contributions": [contribution]}]}

    class Client:
        async def export(self, resource, **params):
            return {"results": [event]}

    monkeypatch.setattr(server, "_client", lambda instance: Client())
    return event


def assert_no_notes(value):
    if isinstance(value, dict):
        assert "note" not in value
        for child in value.values():
            assert_no_notes(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_notes(child)


@pytest.mark.asyncio
async def test_minutes_reach_detailed_tools_by_default(raw_event):
    details = await server.get_event_details(1)
    assert details["note"] == EVENT_NOTE
    assert details["contributions"][0]["note"] == CONTRIBUTION_NOTE
    contributions = await server.get_event_contributions(1)
    assert contributions["items"][0]["note"] == CONTRIBUTION_NOTE
    sessions = await server.get_event_sessions(1)
    assert sessions[0]["note"] == BLOCK_NOTE
    assert sessions[0]["contributions"][0]["note"] == CONTRIBUTION_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [server.get_event_details, server.get_event_contributions, server.get_event_sessions])
async def test_detail_opt_out_suppresses_all_notes_but_keeps_attachments(raw_event, tool):
    result = await tool(1, include_notes=False, include_attachments=True)
    assert_no_notes(result)
    if tool is server.get_event_details:
        contribution = result["contributions"][0]
    elif tool is server.get_event_contributions:
        contribution = result["items"][0]
        assert result["pagination"]["total"] == 1
    else:
        contribution = result[0]["contributions"][0]
    assert contribution["title"] == "Talk"
    assert contribution["attachments"][0]["id"] == 5
    assert raw_event["note"] == EVENT_NOTE
    assert raw_event["contributions"][0]["note"] == CONTRIBUTION_NOTE


@pytest.mark.asyncio
async def test_category_contributions_are_compact_unless_notes_requested(raw_event):
    compact = await server.get_category_contributions(1)
    assert_no_notes(compact)
    assert compact[0]["event_id"] == "1"
    assert compact[0]["title"] == "Talk"
    detailed = await server.get_category_contributions(1, include_notes=True)
    assert detailed[0]["note"] == CONTRIBUTION_NOTE
    assert detailed[0]["event_title"] == "Meeting"
    assert "attachments" not in detailed[0]


@pytest.mark.asyncio
async def test_compact_event_search_still_omits_notes(raw_event):
    assert_no_notes(await server.search_category_events(1))


def test_normalizers_omit_notes_by_default_throughout_nested_results(raw_event):
    assert_no_notes(normalize_event(raw_event, include_contributions=True))
    assert_no_notes(normalize_contribution(raw_event["contributions"][0]))
    assert_no_notes(normalize_session(raw_event["sessions"][0]))


def test_event_note_opt_in_preserves_export_fields():
    assert normalize_event({"note": EVENT_NOTE}, include_note=True)["note"] == EVENT_NOTE
    assert "note" not in normalize_event({"note": None}, include_note=True)


@pytest.mark.parametrize("normalizer", [normalize_event, normalize_contribution, normalize_session])
def test_note_opt_in_keeps_raw_passthrough(normalizer):
    note = {**EVENT_NOTE, "extension": {"synthetic": True}}
    assert normalizer({"note": note}, include_note=True)["note"] is note
