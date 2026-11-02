"""Serve fixture rows through the paged `GET /api/v1/nodes` listing routes."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

# A regex, so `/api/v1/nodes/bulk` and `/api/v1/nodes/move` stay unmatched.
NODES = re.compile(r"/api/v1/nodes\?")
ROOT_FOLDER = {
    "id": "root",
    "name": "/",
    "path": "/",
    "type": "folder",
    "breadcrumbs": [{"id": "root", "name": "/", "path": "/"}],
}


def node_query(request: Any) -> dict[str, str]:
    return {
        key: value[-1] for key, value in parse_qs(urlparse(request.url).query).items()
    }


def is_listing(request: Any, parent: str | None = None) -> bool:
    """A folder listing request, optionally of one `parent_id`."""
    if not NODES.search(request.url):
        return False
    return parent is None or node_query(request).get("parent_id", "/") == parent


def node_page(
    rows: list[dict[str, Any]], query: dict[str, str], folder: dict[str, Any]
) -> dict[str, Any]:
    """One page of `rows` honoring limit, offset, cursor and around."""
    limit = int(query.get("limit", 100))
    cursor = query.get("cursor")
    around = query.get("around")
    if around:
        start = next(i for i, row in enumerate(rows) if row["id"] == around)
    elif cursor:
        start = int(cursor.removeprefix("fixture:"))
    else:
        start = int(query.get("offset", 0))
    end = min(len(rows), start + limit)
    page: dict[str, Any] = {
        "items": rows[start:end],
        "total": len(rows),
        "offset": int(query.get("offset", 0)),
        "next_cursor": f"fixture:{end}" if end < len(rows) else None,
    }
    if around:
        page["index"] = start
    if not cursor:
        page["folder"] = folder
    return page


def fetch_with_rows(route: Any, extra: list[dict[str, Any]]) -> tuple[Any, dict]:
    """The folder's real rows followed by `extra`, paged like the server.

    Type-filtered requests get only the real rows.
    """
    query = node_query(route.request)
    upstream = {
        key: value
        for key, value in query.items()
        if key not in {"cursor", "offset", "around", "limit"}
    }
    url = route.request.url.split("?")[0] + "?" + urlencode({**upstream, "limit": 500})
    response = route.fetch(url=url)
    real = response.json()
    rows = real["items"] + ([] if "type" in query else extra)
    # Folders first, then name: the default order the real listing uses.
    rows.sort(key=lambda row: (row["type"] != "folder", row["name"].casefold()))
    return response, node_page(rows, query, real["folder"])
