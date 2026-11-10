import pytest

from indico_mcp.models import (
    _date_str,
    _person_name,
    extract_results,
    normalize_attachment,
    normalize_contribution,
    normalize_event,
    normalize_event_header,
    normalize_reservation,
    normalize_session,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("2026-01-02", "2026-01-02"),
        ({"date": "2026-01-02", "time": "09:30:00", "tz": "Europe/Zurich"},
         "2026-01-02T09:30:00 (Europe/Zurich)"),
        ({"date": "2026-01-02", "time": "09:30:00"}, "2026-01-02T09:30:00"),
        ({"date": "2026-01-02"}, "2026-01-02"),
        ({}, None),
    ],
)
def test_date_str(value, expected):
    assert _date_str(value) == expected


@pytest.mark.parametrize(
    "person, expected",
    [
        ({"fullName": "Doe, Jane", "first_name": "Ignored"}, "Doe, Jane"),
        ({"first_name": "Jane", "last_name": "Doe"}, "Jane Doe"),
        ({"firstName": "Jane", "lastName": "Doe"}, "Jane Doe"),
        ({"name": "J. Doe"}, "J. Doe"),
        ({}, ""),
    ],
)
def test_person_name(person, expected):
    assert _person_name(person) == expected


def test_normalize_event_flattens_and_drops_empty_fields():
    raw = {
        "id": "42",
        "title": "Group meeting",
        "startDate": {"date": "2026-01-02", "time": "10:00:00", "tz": "UTC"},
        "roomFullname": "Room 1 (Building A)",
        "room": "Room 1",
        "categoryId": 7,
        "description": "",
        "contributions": [{"id": 1, "title": "Talk"}],
    }
    event = normalize_event(raw)
    assert event == {
        "id": "42",
        "title": "Group meeting",
        "start": "2026-01-02T10:00:00 (UTC)",
        "room": "Room 1 (Building A)",
        "category_id": 7,
    }


def test_normalize_event_includes_contributions_only_on_request():
    raw = {"id": "42", "contributions": [{"id": 1, "title": "Talk"}]}
    assert "contributions" not in normalize_event(raw)
    assert normalize_event(raw, include_contributions=True)["contributions"] == [
        {"id": 1, "title": "Talk"}
    ]


def test_normalize_contribution_people_and_session():
    raw = {
        "id": 1,
        "title": "Talk",
        "speakers": [{"fullName": "Doe, Jane"}],
        "primaryauthors": [{"first_name": "John", "last_name": "Roe"}],
        "session": {"id": 3, "title": "Morning"},
        "description": "Abstract text",
        "keywords": [],
    }
    assert normalize_contribution(raw) == {
        "id": 1,
        "title": "Talk",
        "speakers": ["Doe, Jane"],
        "authors": ["John Roe"],
        "session_id": 3,
        "session_title": "Morning",
        "abstract": "Abstract text",
    }


def test_normalize_contribution_plain_session_value():
    contrib = normalize_contribution({"id": 1, "session": "Morning"})
    assert contrib["session_id"] == "Morning"
    assert "session_title" not in contrib


def test_normalize_contribution_attachments_only_on_request():
    raw = {
        "id": 1,
        "folders": [
            {
                "title": "Slides",
                "attachments": [
                    {"id": 9, "title": "talk.pdf", "type": "file",
                     "download_url": "https://indico.example/f/9", "filename": "talk.pdf",
                     "content_type": "application/pdf", "size": 1234},
                ],
            }
        ],
    }
    assert "attachments" not in normalize_contribution(raw)
    [att] = normalize_contribution(raw, include_attachments=True)["attachments"]
    assert att["folder"] == "Slides"
    assert att["filename"] == "talk.pdf"


def test_normalize_session_nests_contributions():
    raw = {
        "id": 3,
        "title": "Morning",
        "conveners": [{"fullName": "Doe, Jane"}],
        "contributions": [{"id": 1, "title": "Talk"}],
    }
    assert normalize_session(raw) == {
        "id": 3,
        "title": "Morning",
        "conveners": ["Doe, Jane"],
        "contributions": [{"id": 1, "title": "Talk"}],
    }


def test_normalize_attachment_file_and_link():
    file_att = normalize_attachment({
        "id": 1, "type": "file", "title": "slides", "filename": "s.pdf",
        "content_type": "application/pdf", "size": 10, "is_protected": False,
        "link_url": "ignored-for-files",
    })
    assert file_att == {
        "id": 1, "type": "file", "title": "slides", "filename": "s.pdf",
        "content_type": "application/pdf", "size": 10,
    }

    link_att = normalize_attachment({
        "id": 2, "type": "link", "link_url": "https://example.org",
        "is_protected": True, "filename": "ignored-for-links",
    })
    assert link_att == {
        "id": 2, "type": "link", "link_url": "https://example.org", "is_protected": True,
    }


def test_normalize_event_header():
    assert normalize_event_header({"id": "5", "title": "Seminar", "startDate": "2026-01-02"}) == {
        "event_id": "5", "event_title": "Seminar", "event_start": "2026-01-02",
    }


def test_normalize_reservation():
    raw = {
        "id": 8,
        "room": {"id": 4, "fullName": "Room 1"},
        "startDT": {"date": "2026-01-02", "time": "10:00:00"},
        "endDT": {"date": "2026-01-02", "time": "11:00:00"},
        "bookedForName": "Jane Doe",
        "isConfirmed": False,
    }
    assert normalize_reservation(raw) == {
        "id": 8, "room_id": 4, "room_name": "Room 1",
        "start": "2026-01-02T10:00:00", "end": "2026-01-02T11:00:00",
        "booked_for": "Jane Doe", "is_confirmed": False,
    }


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"results": [{"id": 1}]}, [{"id": 1}]),
        ({"result": [{"id": 1}]}, [{"id": 1}]),
        ({"results": [[{"id": 1}, {"id": 2}]]}, [{"id": 1}, {"id": 2}]),
        ({"results": []}, []),
        ({}, []),
    ],
)
def test_extract_results(response, expected):
    assert extract_results(response) == expected
