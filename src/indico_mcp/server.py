# Copyright (c) 2026 Christian Ohm. MIT License — see LICENSE file.
"""
Indico MCP Server

Tools:
  search_categories          — find categories by name (returns ID + breadcrumb path)
  find_events_by_title       — search event titles across the whole instance; reveals category IDs
  browse_category            — list direct subcategories of a category by ID
  search_category_events     — list events in a category, filtered by date/keyword
  get_category_contributions — all contributions across events in a category (single call)
  get_event_details          — full event metadata + contributions
  get_event_contributions    — flat list of contributions for an event
  get_event_sessions         — sessions with nested contributions (full agenda)
  search_events_by_keyword   — full-text search via Indico search API
  list_category_info         — metadata about a category, with subcategory names
  list_event_attachments     — list file attachments for an event or contribution
  download_attachment        — download an attachment file to disk
  search_rooms               — find rooms by name and get their IDs (needed for book_room)
  list_room_locations        — list known room booking sites for this instance
  discover_rooms             — scan reservation history to build a local room catalogue
  find_available_rooms       — list rooms not booked in a given time window
  get_room_reservations      — list all reservations in a location within a time window
  book_room                  — create a room booking (requires write:legacy_api token scope)

Configuration (see .env.example):
  INDICO_BASE_URL / INDICO_TOKEN            — single instance
  INDICO_INSTANCES / INDICO_DEFAULT / ...   — multi-instance
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date as date_, datetime as datetime_, timedelta
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import Field

from .client import IndicoClient, IndicoError
from .config import Config
from .models import (
    extract_results,
    normalize_attachment,
    normalize_contribution,
    normalize_event,
    normalize_event_header,
    normalize_reservation,
    normalize_room,
    normalize_session,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

_config: Config | None = None
_clients: dict[str, IndicoClient] = {}


@asynccontextmanager
async def lifespan(server: FastMCP):  # noqa: ARG001
    global _config, _clients
    _config = Config()
    _clients = {
        name: IndicoClient(_config.get(name)) for name in _config.instance_names
    }
    yield
    for c in _clients.values():
        await c.aclose()
    _clients.clear()


app = FastMCP(
    "indico",
    instructions="""
Tools for browsing Indico meeting agendas, searching events, and extracting
contribution and session details across one or more configured Indico instances.
Use the `instance` parameter to select between them.

Important:
- Never assume an instance name like "cern" or "su" exists.
- Only use instance names explicitly configured for this server.
- If unsure, omit `instance` to use the configured default.

## Finding the right category_id

Most tools require a category_id. Use this decision tree when you don't know it:

1. **search_categories** — try first with a keyword from the category name
   (e.g. "colloquia", "HGTD", "Stockholm"). Returns up to 10 matches with full
   breadcrumb paths, so you can confirm you have the right one.

2. **find_events_by_title** — if search_categories fails or returns nothing useful,
   search by a known fragment of the *event* title instead
   (e.g. "AlbaNova ATLAS meeting", "HGTD Speakers Committee meeting").
   Every result includes `category_id` and `category` — this immediately tells you
   which category the events live in, even when the category name is something
   unexpected like "Stockholm" or "HGTD Miscellaneous".

3. **browse_category** — if you have a parent category ID but need to find the right
   subcategory, call this to see all subcategories with event counts. Drill down
   level by level. Start at 0 for the root if completely lost.

## Which instance to search

If a meeting is part of a large international experiment (ATLAS, CMS, LHCb, etc.),
it is almost always on the experiments instance 
(e.g. CERN for the LHC experiment ATLAS, CMS, LHCb and IceCube for IceCube)
even if the group is based elsewhere (Stockholm, Paris, Tokyo). 
Local seminars and colloquia are typically on the institute's own instance.

When selecting an instance, first verify the name is configured in this MCP
deployment.

## Retrieving data efficiently

- **Listing events in a known category:** search_category_events
- **Contributions across many events at once** (speaker counts, theme analysis,
  finding a recurring talk slot): get_category_contributions — one API call instead
  of one call per event. Use this whenever you need to aggregate over a meeting series.
- **Single event agenda:** get_event_details or get_event_sessions
- For very large outputs, prefer small chunks using `limit` + `offset` where available.

## Downloading files

- **List attachments:** list_event_attachments — shows all files (slides, papers, minutes)
  attached to an event or contribution, with download URLs and metadata.
- **Download a file:** download_attachment — downloads a file given its download_url
  (from list_event_attachments) and saves it locally.

## Room booking

- **"What rooms are available on day X from Y to Z?"** — call find_available_rooms.
  The `location` parameter is the site name configured in Indico's room booking module.
  Omit `name_filter` to see all rooms at that location.
- **Find a room by name** — call search_rooms. If a rooms cache exists (built by
  discover_rooms), `location` can be omitted and all locations are searched at once.
- **Browse existing reservations:** get_room_reservations
- **Book a room:** book_room — requires a token with the 'Classic API (read and write)'
  scope. Bookings must start and end on the same day. Some instances require the
  `booked_for` username to be provided explicitly.

## Room location names

The `location` parameter (e.g. "AlbaNova", "Albano Building 3") must match a site
name configured in Indico's room booking module exactly. If you don't know the names:

1. Call list_room_locations — if a rooms cache exists it returns all known locations.
2. Call discover_rooms — scans reservation history across configured locations and
   saves a local catalogue (~/.indico_mcp/{instance}_rooms.json). Offer to run this
   whenever a room operation is requested and no cache file exists.
3. If discover_rooms finds nothing, ask the user: "What is the exact site/location
   name for this room in the Indico room booking interface?" and save the answer to
   memory so it doesn't need to be asked again.
""",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enc(s: str) -> str:
    """Percent-encode a string for safe use as a URL path segment."""
    return urllib.parse.quote(s, safe="")


def _rooms_cache_path(instance_name: str) -> Path:
    cache_dir = Path(os.getenv("INDICO_ROOMS_CACHE_DIR", Path.home() / ".indico_mcp"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{instance_name}_rooms.json"


def _load_rooms_cache(instance_name: str) -> dict | None:
    path = _rooms_cache_path(instance_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _client(instance: str | None) -> IndicoClient:
    if _config is None:
        raise RuntimeError("Indico MCP server is not initialised — lifespan did not run")
    cfg = _config.get(instance)
    return _clients[cfg.name]


def _instance_field() -> Any:
    return Field(
        default=None,
        description=(
            "Named Indico instance to query. Use only configured names. "
            "If omitted, the server default instance is used."
        ),
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@app.tool()
async def list_instances() -> dict:
    """
    List configured Indico instances and the default instance name.

    Use this first if you are unsure which `instance` values are valid.
    """
    if _config is None:
        raise RuntimeError("Indico MCP server is not initialised — lifespan did not run")
    return {
        "default": _config.default_name,
        "instances": _config.instance_names,
    }


@app.tool()
async def search_categories(
    query: Annotated[str, Field(description="Name or partial name of the category to search for.")],
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    Search for Indico categories by name.

    Returns matching categories with their ID, title, full breadcrumb path from the root,
    and event/subcategory counts. Use this to discover the category_id needed by other tools
    when you don't already know it (e.g. 'OKC Colloquia', 'HGTD Speakers Committee').

    Returns up to 10 results. Refine the query if too many or too few results appear.
    """
    client = _client(instance)
    try:
        data = await client.get("category/search", q=query)
    except IndicoError as e:
        raise ValueError(str(e)) from e

    return [
        {k: v for k, v in {
            "id": c.get("id"),
            "title": c.get("title"),
            "path": " > ".join(p["title"] for p in c.get("path", [])),
            "has_events": c.get("has_events"),
            "has_children": c.get("has_children"),
            "event_count": c.get("deep_event_count"),
            "is_protected": c.get("is_protected"),
        }.items() if v is not None}
        for c in data.get("categories", [])
    ]


@app.tool()
async def find_events_by_title(
    title: Annotated[str, Field(description="Event title or partial title to search for.")],
    from_date: Annotated[str | None, Field(description="Start date filter, YYYY-MM-DD.")] = None,
    to_date: Annotated[str | None, Field(description="End date filter, YYYY-MM-DD.")] = None,
    limit: Annotated[int, Field(description="Maximum results to return (default 10).")] = 10,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    Search for events by title across the entire Indico instance.

    Each result includes category_id and category name, making this the most direct
    way to discover which category a meeting series belongs to when you know part of
    the event title but not the category ID.

    Example: searching 'AlbaNova ATLAS meeting' returns events whose category field
    reads 'Stockholm' with category_id=1384, immediately revealing where those
    meetings live so you can pass that ID to other tools.
    """
    client = _client(instance)
    params: dict[str, Any] = {
        "q": title,
        "from": from_date,
        "to": to_date,
        "limit": min(limit, 100),
        "order": "start",
    }
    try:
        data = await client.export("categ/", **params)
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)
    return [normalize_event(r) for r in results[:limit]]


@app.tool()
async def browse_category(
    category_id: Annotated[int, Field(description="Indico category ID. Use 0 for the root.")] = 0,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    List the direct subcategories of an Indico category, with event counts.

    Works by fetching a batch of recent events from the category and collecting the
    distinct (category_id, category_title) pairs from their metadata. This is the
    reliable fallback when the REST API is not available on an instance.

    Use this to navigate the category hierarchy: start at the root (0), pick a
    subcategory, call browse_category again on that ID, and so on until you find
    the right leaf category to pass to search_category_events or get_category_contributions.

    Note: subcategories with no events in the sampled batch may not appear.
    """
    client = _client(instance)
    try:
        data = await client.export(f"categ/{category_id}", limit=500, order="start")
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)

    seen: dict[int, dict] = {}
    for event in results:
        cid = event.get("categoryId")
        ctitle = event.get("category")
        if cid is None:
            continue
        if cid not in seen:
            seen[cid] = {"id": cid, "title": ctitle, "sampled_event_count": 0}
        seen[cid]["sampled_event_count"] += 1

    # Sort by most events first so the busiest subcategories appear at the top
    return sorted(seen.values(), key=lambda x: -x["sampled_event_count"])


@app.tool()
async def get_category_contributions(
    category_id: Annotated[int, Field(description="Indico category ID.")],
    from_date: Annotated[str | None, Field(description="Start date filter, YYYY-MM-DD.")] = None,
    to_date: Annotated[str | None, Field(description="End date filter, YYYY-MM-DD.")] = None,
    limit: Annotated[int, Field(description="Maximum contributions to return (default 200, max 500).")] = 200,
    offset: Annotated[int, Field(description="Pagination offset for retrieving further results.")] = 0,
    include_attachments: Annotated[bool, Field(description="If true, include contribution attachment metadata in each result.")] = False,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    Get contributions from ALL events in a category within an optional date range, in a single API call.

    Returns a flat list of contributions, each annotated with event_id, event_title, and event_start
    so you know which event each contribution belongs to.

    This is far more efficient than listing events and calling get_event_contributions for each one.
    Use it for: finding all talks by a speaker across a series of meetings, analysing themes across
    colloquia, counting talks matching a title pattern, or any aggregation over multiple events.

    The API caps results at 500 contributions per request. Use offset to paginate if needed.
    """
    client = _client(instance)
    params: dict[str, Any] = {
        "from": from_date,
        "to": to_date,
        "limit": min(limit, 500),
        "offset": offset,
        "order": "start",
    }
    try:
        data = await client.export(f"categ/{category_id}", detail="contributions", **params)
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)
    total_count = data.get("count", 0)

    contributions = []
    for event in results:
        event_ctx = normalize_event_header(event)
        for raw_contrib in event.get("contributions", []):
            contrib = normalize_contribution(
                raw_contrib, include_attachments=include_attachments
            )
            contrib.update(event_ctx)
            contributions.append(contrib)

    if not contributions and total_count > 0:
        raise ValueError(
            f"Category {category_id} has {total_count} events but no contributions were "
            "returned. The token most likely lacks the 'Classic API' (legacy_api) scope "
            "required for detail=contributions. Enable it under My Profile → Personal "
            "Tokens on the Indico instance."
        )

    return contributions


@app.tool()
async def search_category_events(
    category_id: Annotated[int, Field(description="Indico category ID. Use 0 for the root (all public events).")] = 0,
    from_date: Annotated[str | None, Field(description="Start date filter, YYYY-MM-DD.")] = None,
    to_date: Annotated[str | None, Field(description="End date filter, YYYY-MM-DD.")] = None,
    keyword: Annotated[str | None, Field(description="Filter event titles by this keyword (case-insensitive).")] = None,
    limit: Annotated[int, Field(description="Maximum number of events to return (default 50, max 1000).")] = 50,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    List events in an Indico category, optionally filtered by date range and keyword.

    Returns a list of events with id, title, type, start/end times, location, and URL.
    Use get_event_details or get_event_contributions to drill into a specific event.
    """
    client = _client(instance)
    params: dict[str, Any] = {
        "from": from_date,
        "to": to_date,
        "q": keyword,  # server-side title filter; applied before limit
        "limit": min(limit, 1000),
        "order": "start",
    }
    try:
        data = await client.export(f"categ/{category_id}", **params)
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)
    return [normalize_event(r) for r in results]


@app.tool()
async def get_event_details(
    event_id: Annotated[int, Field(description="Indico event ID.")],
    include_attachments: Annotated[bool, Field(description="If true, include contribution attachment metadata in the event output.")] = False,
    instance: Annotated[str | None, _instance_field()] = None,
) -> dict:
    """
    Get full metadata for an event, including its list of contributions.

    Returns event title, description, dates, location, and all contributions
    (with speakers, duration, abstract, session assignment).
    """
    client = _client(instance)
    try:
        data = await client.export(f"event/{event_id}", detail="contributions")
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)
    if not results:
        raise ValueError(f"Event {event_id} not found or not accessible.")

    return normalize_event(
        results[0],
        include_contributions=True,
        include_contribution_attachments=include_attachments,
    )


@app.tool()
async def get_event_contributions(
    event_id: Annotated[int, Field(description="Indico event ID.")],
    limit: Annotated[int, Field(description="Maximum contributions to return (default 200, max 1000). Use with offset for paging.")] = 200,
    offset: Annotated[int, Field(description="Pagination offset for retrieving further results.")] = 0,
    include_attachments: Annotated[bool, Field(description="If true, include attachment metadata for each contribution.")] = False,
    instance: Annotated[str | None, _instance_field()] = None,
) -> dict:
    """
    List contributions for an event.

    Each contribution includes: title, speakers, authors, start time, duration,
    session, track, abstract/description, and room.

    Returns both `items` and pagination metadata so partial results are explicit.
    Use `has_more` / `next_offset` to retrieve additional pages.
    """
    client = _client(instance)
    try:
        data = await client.export(f"event/{event_id}", detail="contributions")
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)
    if not results:
        raise ValueError(f"Event {event_id} not found or not accessible.")

    raw_contribs = results[0].get("contributions", [])
    start = max(offset, 0)
    page_limit = min(limit, 1000)
    end = start + page_limit
    items = [
        normalize_contribution(c, include_attachments=include_attachments)
        for c in raw_contribs[start:end]
    ]

    total = len(raw_contribs)
    has_more = end < total
    return {
        "items": items,
        "pagination": {
            "total": total,
            "returned": len(items),
            "limit": page_limit,
            "offset": start,
            "has_more": has_more,
            "next_offset": end if has_more else None,
        },
    }


@app.tool()
async def get_event_sessions(
    event_id: Annotated[int, Field(description="Indico event ID.")],
    include_attachments: Annotated[bool, Field(description="If true, include attachment metadata for contributions inside each session.")] = False,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    Get the session structure for an event, with each session's contributions nested inside.

    Useful for understanding how an event agenda is organised into parallel tracks or blocks.
    Each session includes: title, conveners, start/end time, room, and contributions list.
    """
    client = _client(instance)
    try:
        data = await client.export(f"event/{event_id}", detail="sessions")
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)
    if not results:
        raise ValueError(f"Event {event_id} not found or not accessible.")

    raw_sessions = results[0].get("sessions", [])
    return [
        normalize_session(s, include_attachments=include_attachments)
        for s in raw_sessions
    ]


@app.tool()
async def search_events_by_keyword(
    keyword: Annotated[str, Field(description="Search term.")],
    limit: Annotated[int, Field(description="Maximum results to return (default 20).")] = 20,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    Full-text search for events matching a keyword.

    Uses the Indico search API if available, otherwise falls back to the
    legacy title-search export endpoint. Returns matching events with id,
    title, category, dates, and URL.
    """
    client = _client(instance)

    # Try the modern search API first
    try:
        data = await client.api("search/", q=keyword, scope="events", limit=limit)
        # Modern search API returns {"results": {"events": {"results": [...], "total": n}}}
        events_block = (
            data.get("results", {}).get("events", {})
            if isinstance(data.get("results"), dict)
            else {}
        )
        raw_events = events_block.get("results", [])
        if raw_events:
            return [normalize_event(e) for e in raw_events[:limit]]
    except IndicoError as e:
        if e.status_code != 404:
            raise ValueError(str(e)) from e
        # 404: modern search endpoint not available on this instance, fall through to legacy

    # Legacy: search via the export title-search endpoint
    try:
        encoded = urllib.parse.quote(keyword, safe="")
        data = await client.export(f"event/search/{encoded}", limit=limit)
        results = extract_results(data)
        return [normalize_event(r) for r in results[:limit]]
    except IndicoError as e:
        raise ValueError(str(e)) from e


@app.tool()
async def list_category_info(
    category_id: Annotated[int, Field(description="Indico category ID. Use 0 for the root.")] = 0,
    instance: Annotated[str | None, _instance_field()] = None,
) -> dict:
    """
    Get metadata about an Indico category: name, description, and subcategory IDs.

    Useful for navigating the category hierarchy to find the right category_id
    before using search_category_events.
    """
    client = _client(instance)

    # Try the REST API first (returns richer category data)
    try:
        data = await client.api(f"categories/{category_id}/")
        return {k: v for k, v in {
            "id": data.get("id"),
            "title": data.get("title"),
            "description": data.get("description") or None,
            "url": data.get("url"),
            "parent_id": data.get("parent_id"),
            "subcategories": [
                {"id": c.get("id"), "title": c.get("title")}
                for c in data.get("subcategories", [])
            ] or None,
        }.items() if v is not None}
    except IndicoError as e:
        if e.status_code != 404:
            raise ValueError(str(e)) from e
        # 404: REST categories endpoint not available on this instance, fall through to export

    # Fallback: infer from a minimal export call
    try:
        data = await client.export(f"categ/{category_id}", limit=0)
    except IndicoError as e:
        raise ValueError(str(e)) from e

    return {
        "id": category_id,
        "count": data.get("count", 0),
        "note": (
            "Full category metadata not available on this instance. "
            "Use search_category_events to list events."
        ),
    }


@app.tool()
async def list_event_attachments(
    event_id: Annotated[int, Field(description="Indico event ID.")],
    contribution_id: Annotated[int | None, Field(description="If set, only list attachments for this contribution.")] = None,
    limit: Annotated[int, Field(description="Maximum attachments to return (default 200, max 1000). Use with offset for paging.")] = 200,
    offset: Annotated[int, Field(description="Pagination offset for retrieving further results.")] = 0,
    instance: Annotated[str | None, _instance_field()] = None,
) -> dict:
    """
    List all file attachments and links for an event (or a specific contribution).

    Returns a flat list of attachments, each with: id, title, filename, content_type,
    size, download_url, and the folder/contribution/event context it belongs to.

    Use this to discover what files (slides, papers, minutes, etc.) are attached to
    an event or contribution before downloading them with download_attachment.

    Returns both `items` and pagination metadata so partial results are explicit.
    Use `has_more` / `next_offset` to retrieve additional pages.
    """
    client = _client(instance)
    try:
        data = await client.export(f"event/{event_id}", detail="contributions")
    except IndicoError as e:
        raise ValueError(str(e)) from e

    results = extract_results(data)
    if not results:
        raise ValueError(f"Event {event_id} not found or not accessible.")

    event = results[0]
    attachments: list[dict] = []

    def _collect(obj: dict, context: dict) -> None:
        for folder in obj.get("folders", []):
            folder_title = folder.get("title", "")
            for att in folder.get("attachments", []):
                entry = normalize_attachment(att)
                entry["folder"] = folder_title
                entry.update(context)
                attachments.append(entry)

    # Event-level attachments
    _collect(event, {"event_id": event_id})

    # Contribution-level attachments
    for contrib in event.get("contributions", []):
        cid = contrib.get("id")
        if contribution_id is not None and cid != contribution_id:
            continue
        ctx = {"event_id": event_id, "contribution_id": cid, "contribution_title": contrib.get("title")}
        _collect(contrib, ctx)

        # Subcontribution-level attachments
        for subcontrib in contrib.get("subContributions", []):
            sub_ctx = {**ctx, "subcontribution_id": subcontrib.get("id"), "subcontribution_title": subcontrib.get("title")}
            _collect(subcontrib, sub_ctx)

    start = max(offset, 0)
    page_limit = min(limit, 1000)
    end = start + page_limit
    items = attachments[start:end]
    total = len(attachments)
    has_more = end < total

    note: str | None = None
    if total == 0:
        note = "No attachments found for this event." + (
            f" (filtered to contribution {contribution_id})" if contribution_id else ""
        )

    return {
        "items": items,
        "pagination": {
            "total": total,
            "returned": len(items),
            "limit": page_limit,
            "offset": start,
            "has_more": has_more,
            "next_offset": end if has_more else None,
        },
        "note": note,
    }


@app.tool()
async def download_attachment(
    download_url: Annotated[str, Field(description="The download_url from list_event_attachments output.")],
    save_to: Annotated[str | None, Field(
        description="Local file path to save the file to. If not provided, saves to a temp directory."
    )] = None,
    instance: Annotated[str | None, _instance_field()] = None,
) -> dict:
    """
    Download a file attachment from Indico and save it locally.

    Use list_event_attachments first to get the download_url for the file you want.
    Returns the local file path, filename, content type, and size.

    Files are saved to a temporary directory by default, or to a specified path.
    Maximum file size: 100 MB.
    """
    client = _client(instance)
    try:
        result = await client.download(download_url)
    except IndicoError as e:
        raise ValueError(str(e)) from e

    if save_to:
        dest = Path(save_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        tmp_dir = Path(tempfile.gettempdir()) / "indico-mcp-downloads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dest = tmp_dir / result.filename

    # Avoid overwriting: append a suffix if needed
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = dest.with_name(f"{stem}_{counter}{suffix}")
            counter += 1

    dest.write_bytes(result.content)

    return {
        "path": str(dest),
        "filename": result.filename,
        "content_type": result.content_type,
        "size": result.size,
    }


# ---------------------------------------------------------------------------
# Room booking tools
# ---------------------------------------------------------------------------


@app.tool()
async def list_room_locations(
    instance: Annotated[str | None, _instance_field()] = None,
) -> dict:
    """
    List all room booking locations (sites) known for this Indico instance.

    Returns locations from the rooms cache (built by discover_rooms) if it exists,
    otherwise returns the locations configured via INDICO_*_ROOM_LOCATIONS.
    The location name is required by search_rooms, find_available_rooms,
    get_room_reservations, and book_room.

    When reading from the cache the response also includes 'cache_updated'
    (ISO date the cache was last refreshed) and 'cache_age_days' so the caller
    can decide whether to re-run discover_rooms.
    """
    cfg = _config.get(instance) if _config else None
    instance_name = cfg.name if cfg else "default"

    cache = _load_rooms_cache(instance_name)
    if cache:
        updated_str = cache.get("updated", "")
        age_days: int | None = None
        if updated_str:
            try:
                age_days = (date_.today() - date_.fromisoformat(updated_str)).days
            except ValueError:
                pass
        result: dict = {"locations": sorted(cache.get("locations", {}).keys())}
        if updated_str:
            result["cache_updated"] = updated_str
        if age_days is not None:
            result["cache_age_days"] = age_days
        return result

    if cfg and cfg.room_locations:
        return {"locations": sorted(cfg.room_locations)}

    raise ValueError(
        "No rooms cache found and no INDICO_*_ROOM_LOCATIONS configured. "
        "Run discover_rooms to build a catalogue, or set room locations in the env config."
    )


@app.tool()
async def discover_rooms(
    locations: Annotated[list[str] | None, Field(
        description="Location names to scan. If omitted, uses INDICO_*_ROOM_LOCATIONS from config."
    )] = None,
    instance: Annotated[str | None, _instance_field()] = None,
) -> str:
    """
    Scan reservation history to build a room catalogue and save it locally.

    Fetches reservations across a broad time window (past 2 years + next 6 months)
    for each location to discover all rooms that have ever been booked, then saves
    the catalogue to ~/.indico_mcp/{instance}_rooms.json (override with
    INDICO_ROOMS_CACHE_DIR). After running this, search_rooms works without needing
    a location argument.

    If no locations are provided and none are configured, ask the user for the
    site/location names shown in the Indico room booking interface.
    """
    client = _client(instance)
    cfg = _config.get(instance) if _config else None
    instance_name = cfg.name if cfg else "default"

    scan_locations = locations or (cfg.room_locations if cfg else [])
    if not scan_locations:
        raise ValueError(
            "No locations to scan. Provide a list of location names, or set "
            "INDICO_*_ROOM_LOCATIONS in the env config. Ask the user for the exact "
            "site names shown in the Indico room booking interface if unknown."
        )

    today = date_.today()
    from_dt = (today - timedelta(days=730)).isoformat() + "T00:00"
    to_dt = (today + timedelta(days=180)).isoformat() + "T23:59"

    catalogue: dict[str, list[dict]] = {}
    errors: list[str] = []

    for loc in scan_locations:
        try:
            data = await client.export(
                f"reservation/{_enc(loc)}",
                **{"from": from_dt, "to": to_dt},
            )
        except IndicoError as e:
            errors.append(f"{loc}: {e}")
            continue

        seen: dict[int, dict] = {}
        for raw in extract_results(data):
            room = raw.get("room", {})
            if not isinstance(room, dict):
                continue
            rid = room.get("id")
            rname = room.get("fullName")
            if rid is not None and rname is not None and rid not in seen:
                seen[rid] = {"id": rid, "full_name": rname}

        catalogue[loc] = sorted(seen.values(), key=lambda r: r["full_name"])

    # Merge with existing cache so previous locations are preserved
    cache_path = _rooms_cache_path(instance_name)
    existing = _load_rooms_cache(instance_name) or {}
    merged_locations = {**existing.get("locations", {}), **catalogue}
    cache = {
        "instance": instance_name,
        "base_url": cfg.base_url if cfg else "",
        "updated": today.isoformat(),
        "locations": merged_locations,
    }
    # Write atomically: write to a temp file first, then rename into place.
    # This prevents a corrupt half-written cache if the process is interrupted.
    tmp_path = cache_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(cache, indent=2))
    tmp_path.rename(cache_path)

    total = sum(len(v) for v in catalogue.values())
    summary = f"Discovered {total} rooms across {len(catalogue)} location(s) → {cache_path}\n"
    for loc, rooms in catalogue.items():
        summary += f"  {loc}: {len(rooms)} room(s)\n"
    if errors:
        summary += "Errors:\n" + "\n".join(f"  {e}" for e in errors)
    return summary.strip()


@app.tool()
async def search_rooms(
    name_filter: Annotated[str | None, Field(description="Substring to filter room names (case-insensitive).")] = None,
    location: Annotated[str | None, Field(description="Limit search to this location. If omitted, all cached locations are searched.")] = None,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    Find rooms by name and return their numeric IDs.

    If a rooms cache exists (built by discover_rooms), searches it across all
    locations without any API call. Falls back to scanning reservation history
    live if no cache exists, in which case `location` is required.

    Each result includes the room id (needed by find_available_rooms and book_room),
    full_name, and location.

    Example: search_rooms("Xenon") finds all rooms with "Xenon" in the name across
    every cached location.
    """
    cfg = _config.get(instance) if _config else None
    instance_name = cfg.name if cfg else "default"

    # Fast path: use the local cache (only when location is absent or already cached)
    cache = _load_rooms_cache(instance_name)
    cached_locations = set(cache.get("locations", {}).keys()) if cache else set()
    use_cache = cache and (not location or location in cached_locations)
    if use_cache:
        results = []
        for loc, rooms in cache.get("locations", {}).items():  # type: ignore[union-attr]
            if location and loc.lower() != location.lower():
                continue
            for r in rooms:
                if name_filter is None or name_filter.lower() in r["full_name"].lower():
                    results.append({**r, "location": loc})
        # When the requested location is definitively present in the cache, trust
        # the cache result (returning an empty list if nothing matched). Only fall
        # through to the live API when no location was specified and the cache
        # returned nothing across all stored locations.
        if results or location:
            return sorted(results, key=lambda r: r["full_name"])
        # location=None and cache returned nothing — fall through to live API

    # Slow path: live API scan (requires location)
    if not location:
        raise ValueError(
            "No rooms cache found. Either run discover_rooms first, or provide "
            "a location name to search live (e.g. search_rooms(location='AlbaNova'))."
        )

    client = _client(instance)

    # Primary: roomName export — searches rooms directly by name, no booking history needed.
    # Only usable when a name_filter is given (empty name segment causes a 404).
    if name_filter:
        try:
            rooms_data = await client.export(
                f"roomName/{_enc(location)}/{_enc(name_filter)}", limit=500
            )
            rooms = [
                {"id": r["id"], "full_name": r.get("full_name") or r.get("name", ""), "location": location}
                for r in (normalize_room(raw) for raw in extract_results(rooms_data))
                if r.get("id") is not None
            ]
            if rooms:
                return sorted(rooms, key=lambda r: r["full_name"])
        except IndicoError:
            pass  # fall through to reservation-based scan

    # Fallback: scan reservation history — finds all rooms regardless of name,
    # but only rooms with at least one booking in the past year appear.
    today = date_.today()
    from_dt = (today - timedelta(days=365)).isoformat() + "T00:00"
    to_dt = (today + timedelta(days=90)).isoformat() + "T23:59"

    try:
        data = await client.export(
            f"reservation/{_enc(location)}",
            **{"from": from_dt, "to": to_dt},
        )
    except IndicoError as e:
        raise ValueError(str(e)) from e

    seen: dict[int, dict] = {}
    for raw in extract_results(data):
        room = raw.get("room", {})
        if not isinstance(room, dict):
            continue
        room_id = room.get("id")
        room_name = room.get("fullName")
        if room_id is None or room_name is None:
            continue
        if name_filter and name_filter.lower() not in room_name.lower():
            continue
        if room_id not in seen:
            seen[room_id] = {"id": room_id, "full_name": room_name, "location": location}

    if not seen:
        msg = f"No rooms found in location '{location}'"
        if name_filter:
            msg += f" matching '{name_filter}'"
        msg += ". The location may have no bookings in the past year, or the name may be wrong."
        raise ValueError(msg)

    return sorted(seen.values(), key=lambda r: r["full_name"])


@app.tool()
async def find_available_rooms(
    location: Annotated[str, Field(description="Indico location/building name (e.g. 'AlbaNova', 'CERN').")],
    date: Annotated[str, Field(description="Date to check, YYYY-MM-DD.")],
    from_time: Annotated[str, Field(description="Start of desired window, HH:MM (24-hour).")],
    to_time: Annotated[str, Field(description="End of desired window, HH:MM (24-hour).")],
    name_filter: Annotated[str | None, Field(description="Optional room name fragment to narrow the search.")] = None,
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    List rooms that are NOT already booked in a given time window.

    Discovers known rooms from reservation history (past year + next 3 months), then
    checks which are booked in the requested window. Returns rooms with no conflict,
    each with its numeric id (needed for book_room) and full_name.

    Use name_filter to narrow to a specific room or building wing.

    Note: room discovery relies on past booking history. Rooms that have never been
    booked in the past year will not appear in the results even if they are free.
    If you believe a room is missing, try passing its name (or a fragment) via
    name_filter, which triggers an additional direct room-name lookup.
    """
    client = _client(instance)

    # Validate date and time inputs before making any API calls.
    try:
        datetime_.strptime(date, "%Y-%m-%d")
        datetime_.strptime(from_time, "%H:%M")
        datetime_.strptime(to_time, "%H:%M")
    except ValueError as exc:
        raise ValueError(
            f"Invalid date or time format: {exc}. "
            "Expected date=YYYY-MM-DD, from_time/to_time=HH:MM."
        ) from exc
    if from_time >= to_time:
        raise ValueError(
            f"from_time ({from_time}) must be earlier than to_time ({to_time})."
        )

    from_dt = f"{date}T{from_time}"
    to_dt = f"{date}T{to_time}"

    # Discover all known rooms from a broad reservation window, and check
    # conflicts in the specific window — two calls in parallel.
    today = date_.today()
    broad_from = (today - timedelta(days=365)).isoformat() + "T00:00"
    broad_to = (today + timedelta(days=90)).isoformat() + "T23:59"

    try:
        broad_data, window_data = await asyncio.gather(
            client.export(f"reservation/{_enc(location)}", **{"from": broad_from, "to": broad_to}),
            client.export(f"reservation/{_enc(location)}", **{"from": from_dt, "to": to_dt,
                                                              "cancelled": "no", "rejected": "no"}),
        )
    except IndicoError as e:
        raise ValueError(
            f"Could not fetch reservations for '{location}': {e}. "
            "Check the location name and that the token has the 'Classic API' scope."
        ) from e

    # Build room catalogue from broad history
    all_rooms: dict[int, dict] = {}
    for raw in extract_results(broad_data):
        room = raw.get("room", {})
        if not isinstance(room, dict):
            continue
        rid = room.get("id")
        rname = room.get("fullName")
        if rid is None or rname is None:
            continue
        if name_filter and name_filter.lower() not in rname.lower():
            continue
        if rid not in all_rooms:
            all_rooms[rid] = {"id": rid, "full_name": rname}

    if not all_rooms and name_filter:
        # Reservation history found nothing — try roomName export directly
        try:
            rooms_data = await client.export(
                f"roomName/{_enc(location)}/{_enc(name_filter)}", limit=500
            )
            for raw in extract_results(rooms_data):
                r = normalize_room(raw)
                rid = r.get("id")
                rname = r.get("full_name") or r.get("name")
                if rid is not None and rname is not None:
                    all_rooms[rid] = {"id": rid, "full_name": rname}
        except IndicoError:
            pass

    if not all_rooms:
        raise ValueError(
            f"No rooms found in location '{location}'"
            + (f" matching '{name_filter}'" if name_filter else "")
            + ". The location may be wrong, or no reservations exist in the past year."
        )

    # Find rooms booked in the requested window
    booked_ids = {
        raw.get("room", {}).get("id")
        for raw in extract_results(window_data)
        if isinstance(raw.get("room"), dict)
    }

    return [r for r in sorted(all_rooms.values(), key=lambda r: r["full_name"])
            if r["id"] not in booked_ids]


@app.tool()
async def get_room_reservations(
    location: Annotated[str, Field(description="Indico location/building name.")],
    from_dt: Annotated[str, Field(description="Start of window, YYYY-MM-DDTHH:MM.")],
    to_dt: Annotated[str, Field(description="End of window, YYYY-MM-DDTHH:MM.")],
    instance: Annotated[str | None, _instance_field()] = None,
) -> list[dict]:
    """
    List all confirmed room reservations in a location within a time window.

    Each reservation includes: room name, start/end time, who it is booked for, and reason.
    Requires a token with the 'Classic API' (legacy_api) scope.
    """
    client = _client(instance)
    try:
        data = await client.export(
            f"reservation/{_enc(location)}",
            **{"from": from_dt, "to": to_dt, "cancelled": "no", "rejected": "no"},
        )
    except IndicoError as e:
        raise ValueError(str(e)) from e

    return [normalize_reservation(r) for r in extract_results(data)]


@app.tool()
async def book_room(
    room_id: Annotated[int, Field(description="Indico room ID (from search_rooms or find_available_rooms).")],
    from_dt: Annotated[str, Field(description="Booking start, YYYY-MM-DDTHH:MM.")],
    to_dt: Annotated[str, Field(description="Booking end, YYYY-MM-DDTHH:MM. Must be on the same day as start.")],
    reason: Annotated[str, Field(description="Reason for the booking.")],
    booked_for: Annotated[str | None, Field(
        description=(
            "Indico username to book on behalf of (defaults to the token owner). "
            "Only pass this when the user has explicitly asked to book on behalf of "
            "someone else — misuse may constitute impersonation."
        )
    )] = None,
    dry_run: Annotated[bool, Field(
        description=(
            "When True, validate all inputs and return the booking parameters that "
            "would be submitted without actually creating the booking. "
            "Use this to confirm details with the user before committing."
        )
    )] = False,
    instance: Annotated[str | None, _instance_field()] = None,
) -> dict:
    """
    Create a room booking in Indico.

    Always confirm the full details with the user before calling this tool
    with dry_run=False, as the booking is created immediately and may be
    difficult to cancel.

    IMPORTANT CONSTRAINTS:
    - The booking must start and end on the same day (Indico API limitation for
      this endpoint; recurring or multi-day bookings must be made via the web UI).
    - The token must have the 'Classic API (read and write)' scope.
    - Booking dates can be today or in the future; exact restrictions depend on
      the Indico instance's room booking policy.

    RETRY SAFETY: this endpoint is not idempotent. Submitting the same request
    twice (e.g. after a transient network error) will create two separate bookings.
    Use dry_run=True to pre-validate before committing, and do not retry
    automatically on failure.

    IMPERSONATION: the booked_for parameter allows booking on behalf of another
    user. Only use it when the user has explicitly authorised this action.

    Returns the booking confirmation from Indico, including the reservation ID if
    successful. When dry_run=True, returns the parameters that would have been sent
    without making any API call.
    """
    # Validate datetime inputs regardless of dry_run.
    try:
        start = datetime_.fromisoformat(from_dt)
        end = datetime_.fromisoformat(to_dt)
    except ValueError as exc:
        raise ValueError(
            f"Invalid datetime format: {exc}. Expected YYYY-MM-DDTHH:MM."
        ) from exc
    if start >= end:
        raise ValueError(
            f"Booking start ({from_dt}) must be before booking end ({to_dt})."
        )
    if start.date() != end.date():
        raise ValueError(
            f"Start ({from_dt}) and end ({to_dt}) must be on the same day. "
            "Multi-day bookings must be created via the Indico web interface."
        )

    # Build the API payload first; booking_params derives from it for dry_run output.
    api_params: dict[str, Any] = {
        "roomid": room_id,
        "from": from_dt,
        "to": to_dt,
        "reason": reason,
    }
    if booked_for is not None:
        api_params["username"] = booked_for

    if dry_run:
        # Return a user-readable summary rather than the raw API key names.
        display = {
            "room_id": room_id,
            "from": from_dt,
            "to": to_dt,
            "reason": reason,
        }
        if booked_for is not None:
            display["booked_for"] = booked_for
        return {"dry_run": True, "would_book": display}

    client = _client(instance)
    try:
        return await client.post_form("roomBooking/bookRoom.json", **api_params)
    except IndicoError as e:
        raise ValueError(str(e)) from e


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
