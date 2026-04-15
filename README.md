# indico-mcp

An MCP (Model Context Protocol) server for the [Indico](https://getindico.io/) meeting agenda system, letting AI agents search event categories, browse agendas, and extract contribution and session details.

Works with any Indico instance — configure multiple instances simultaneously and switch between them per tool call.

## Tools

| Tool | Description |
|------|-------------|
| `list_instances` | List configured instance names and the current default instance |
| `search_categories` | Find categories by name; returns ID, breadcrumb path, and event count |
| `find_events_by_title` | Search event titles across the whole instance; each result includes `category_id` — useful for discovering which category a meeting series belongs to |
| `browse_category` | List direct subcategories of a category by ID; works without the REST API |
| `search_category_events` | List events in a category, filtered by date range and keyword |
| `get_category_contributions` | All contributions from every event in a category within a date range, in a single API call |
| `get_event_details` | Full event metadata including all contributions |
| `get_event_contributions` | Paginated contributions (`items` + `pagination`): speakers, abstract, duration, track, session |
| `get_event_sessions` | Session structure with nested contributions (full agenda view) |
| `search_events_by_keyword` | Full-text search across events |
| `list_category_info` | Category name, description, and direct subcategories with names |
| `list_event_attachments` | Paginated attachments (`items` + `pagination`) for an event or contribution |
| `download_attachment` | Download an attachment file to disk given its download URL |
| `list_room_locations` | List known room booking sites for this instance |
| `discover_rooms` | Scan reservation history to build a local room catalogue |
| `search_rooms` | Find rooms by name and get their numeric IDs (needed for booking) |
| `find_available_rooms` | List rooms not already booked in a given time window |
| `get_room_reservations` | List all confirmed reservations in a location within a time window |
| `book_room` | Create a room booking (requires Classic API write scope; same-day only) |

All tools accept an optional `instance` parameter to select which Indico server to query.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Installation

```bash
git clone <repo>
cd indico-mcp
uv sync
```

## Configuration

Copy the example env file and fill in your details:

```bash
cp .env.example .env
```

### Single instance

```
INDICO_BASE_URL=https://indico.fysik.su.se
INDICO_TOKEN=indp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### Multiple instances

```
INDICO_INSTANCES=cern,su
INDICO_DEFAULT=su

INDICO_CERN_URL=https://indico.cern.ch
INDICO_CERN_TOKEN=indp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

INDICO_SU_URL=https://indico.fysik.su.se
INDICO_SU_TOKEN=indp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`INDICO_DEFAULT` sets which instance is used when no `instance=` argument is passed to a tool. Tokens are optional for public-only access.

### Room booking setup

Room booking tools (`search_rooms`, `find_available_rooms`, `book_room`) require knowing the **site name** as configured in Indico's room booking module. These names are not discoverable via the API — you must look them up in the Indico room booking interface (they appear as location headings, e.g. `"AlbaNova Main Building"`, `"Albano Building 3"`).

#### 1. Configure site names

Add the known site names to your `.env`:

```
INDICO_SU_ROOM_LOCATIONS=AlbaNova Main Building,Albano Building 3
```

For single-instance: `INDICO_ROOM_LOCATIONS=Main Building,Building 40`

#### 2. Run room discovery

Once site names are configured, run `discover_rooms` once (e.g. by asking your agent to do it). This scans reservation history across all configured sites and saves a local room catalogue to `~/.indico_mcp/{instance}_rooms.json` (override the directory with `INDICO_ROOMS_CACHE_DIR`).

After discovery, `search_rooms` finds rooms by partial name across all catalogued sites without needing a location argument. Rooms with no booking history are found on-the-fly via Indico's room name search endpoint, so discovery does not need to be exhaustive.

Re-run `discover_rooms` periodically (or whenever new rooms are added) to keep the catalogue fresh. The agent will offer to run it automatically if the cache file is missing when a room operation is requested.

### Getting a token

1. Log in to your Indico instance
2. Go to **My Profile → Personal Tokens** (or `/user/tokens/`)
3. Click **Create token**
4. Give it a name and enable the **Classic API (read)** scope — this grants read access to the event export API
5. If you also need write access (creating events, bookings), enable **Classic API (read and write)** instead
6. Copy the generated token (starts with `indp_`)

> **Note on scope naming:** The scope is labelled "Classic API" in the UI but this refers to Indico's HTTP Export API (`/export/`), which is the only way to programmatically read event and contribution data. Despite the name, using it with a modern Bearer token (`indp_...`) is the current recommended approach — the deprecated system is the old API *key* (a separate, shorter token found under My Profile → HTTP API).

## Connecting to Claude

Run `claude add indico -- uv run --directory /path/to/indico-mcp indico-mcp` or manually add to your Claude config (`~/.claude.json` or Claude Desktop settings):

```json
{
  "mcpServers": {
    "indico": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/indico-mcp", "indico-mcp"]
    }
  }
}
```

The `.env` file in the project directory is loaded automatically. Alternatively, pass configuration directly via the `env` block in the MCP config:

```json
{
  "mcpServers": {
    "indico": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/indico-mcp", "indico-mcp"],
      "env": {
        "INDICO_INSTANCES": "cern,su",
        "INDICO_DEFAULT": "su",
        "INDICO_CERN_URL": "https://indico.cern.ch",
        "INDICO_CERN_TOKEN": "indp_xxx",
        "INDICO_SU_URL": "https://indico.fysik.su.se",
        "INDICO_SU_TOKEN": "indp_yyy"
      }
    }
  }
}
```

## Usage examples

```python
# Events in the next month (default instance)
search_category_events(from_date="2025-04-01", to_date="2025-04-30")

# Events in a specific category on CERN Indico
search_category_events(category_id=72, instance="cern")

# First page of contributions for a meeting
get_event_contributions(event_id=1234567, instance="cern")

# Continue with paging using `pagination.next_offset`
get_event_contributions(event_id=1234567, limit=100, offset=0, instance="cern")

# Full session/agenda structure of a conference
get_event_sessions(event_id=9876543, instance="su")

# Full-text search
search_events_by_keyword("dark matter", instance="cern")

# Navigate the category hierarchy
list_category_info(category_id=0, instance="su")

# First page of attachments for an event
list_event_attachments(event_id=1234567, instance="cern")

# Continue with paging using `pagination.next_offset`
list_event_attachments(event_id=1234567, limit=100, offset=0, instance="cern")

# List attachments for a specific contribution
list_event_attachments(event_id=1234567, contribution_id=42, instance="cern")

# Download a file (URL from list_event_attachments output)
download_attachment(download_url="https://indico.cern.ch/event/.../file.pdf")
```

## How it works

The server uses the [Indico HTTP Export API](https://docs.getindico.io/en/stable/http-api/) (`/export/`) with `detail=contributions` and `detail=sessions` query parameters to retrieve structured agenda data. Authentication uses a standard `Authorization: Bearer <token>` header. The `/api/` endpoints in Indico are write-only (POST); all read operations go through `/export/`.

File attachments are discovered via the `folders` structure included in the export API response, which provides direct download URLs, filenames, content types, and sizes. Downloads are authenticated with the same Bearer token and saved locally (to a temp directory by default, or a specified path). Maximum file size is 100 MB.

## Contributing

Feature requests and bug reports are welcome. Contributions are especially encouraged from Indico users who can test new functionality against a real instance before submitting a pull request — the Indico API has enough instance-to-instance variation that untested changes are hard to review reliably.

Substantial contributions will be recognised by adding the contributor as an author.

## License

Copyright (c) 2026 Christian Ohm. MIT License — see the [LICENSE](LICENSE) file.

Indico itself is also [MIT licensed](https://github.com/indico/indico/blob/master/LICENSE), so this can perhaps be absorbed into that repository.
