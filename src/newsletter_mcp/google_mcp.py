"""Read-only local MCP servers for Google Drive, Gmail, and Calendar.

These servers deliberately expose only the small tool surface used by the
newsletter pipeline.  OAuth is owned by the dashboard; each subprocess
receives only a short-lived access token through ``GOOGLE_ACCESS_TOKEN``.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from newsletter_mcp.protocol import ToolFailure, ToolServer

GOOGLE_API = "https://www.googleapis.com"
DRIVE_DOC = "application/vnd.google-apps.document"
DRIVE_SHEET = "application/vnd.google-apps.spreadsheet"
DRIVE_SLIDES = "application/vnd.google-apps.presentation"
MAX_DRIVE_NAMES = 20
MAX_GMAIL_TEXT = 12_000


def _bounded_int(value: int, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or value < minimum or value > maximum:
        raise ToolFailure(f"{label} must be between {minimum} and {maximum}")
    return value


def _required(value: str, *, maximum: int, label: str) -> str:
    value = value.strip()
    if not value or len(value) > maximum:
        raise ToolFailure(f"{label} must contain 1 to {maximum} characters")
    return value


def _drive_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _decode_base64url(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


class GoogleAPI:
    """Small authenticated Google API client with bounded idempotent retries."""

    def __init__(self, token: str | None = None, client: httpx.AsyncClient | None = None):
        self.token = token if token is not None else os.getenv("GOOGLE_ACCESS_TOKEN", "")
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        if not self.token:
            raise ToolFailure("Google authorization required; connect Google Workspace in Settings")
        for attempt in range(3):
            try:
                response = await self.client.get(
                    GOOGLE_API + path,
                    params=params,
                    headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
                )
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise ToolFailure("Google API could not be reached") from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = min(max(float(retry_after), 0.0), 3.0)
            except ValueError:
                delay = 0.25 * (2**attempt)
            await asyncio.sleep(delay)
        if response.status_code == 401:
            raise ToolFailure("Google authorization expired; reconnect Google Workspace in Settings")
        if response.status_code == 403:
            raise ToolFailure("Google API access denied; enable the API and grant its read-only scope")
        if response.status_code == 404:
            raise ToolFailure("The requested Google resource was not found or is not shared with this account")
        if response.status_code == 429:
            raise ToolFailure("Google API rate limit reached; retry later")
        if response.status_code >= 500:
            raise ToolFailure("Google API is temporarily unavailable")
        if response.status_code >= 400:
            raise ToolFailure(f"Google API request failed (HTTP {response.status_code})")
        return response


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise ToolFailure("Google API returned an invalid response") from exc
    if not isinstance(value, dict):
        raise ToolFailure("Google API returned an unexpected response")
    return value


def _mime_text(payload: dict[str, Any]) -> str:
    """Extract message text without downloading attachments."""
    plain: list[str] = []
    html: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        if part.get("filename") or body.get("attachmentId"):
            return
        mime = str(part.get("mimeType", "")).lower()
        data = body.get("data")
        if isinstance(data, str):
            decoded = _decode_base64url(data)
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(decoded)
        for child in part.get("parts", []):
            if isinstance(child, dict):
                visit(child)

    visit(payload)
    if plain:
        return "\n\n".join(piece.strip() for piece in plain if piece.strip())[:MAX_GMAIL_TEXT]
    if html:
        soup = BeautifulSoup("\n".join(html), "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        return soup.get_text(" ", strip=True)[:MAX_GMAIL_TEXT]
    return ""


def _header(headers: Any, name: str) -> str:
    if not isinstance(headers, list):
        return ""
    for item in headers:
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
            return str(item.get("value", ""))[:500]
    return ""


def server_for(name: str, *, api: GoogleAPI | None = None) -> ToolServer:
    if name not in {"google_drive", "gmail", "google_calendar"}:
        raise ValueError("Unknown Google MCP service")
    service = api or GoogleAPI()
    server = ToolServer(name)

    @server.tool()
    async def status() -> dict:
        """Report whether this local Google MCP received an OAuth access token."""
        return {"ready": bool(service.token), "service": name, "read_only": True}

    if name == "google_drive":
        @server.tool()
        async def search_files(names: list[str], folder_id: str = "", max_results: int = 20) -> dict:
            """Find non-trashed Drive files by exact name, optionally inside one folder."""
            if not names or len(names) > MAX_DRIVE_NAMES:
                raise ToolFailure(f"names must contain 1 to {MAX_DRIVE_NAMES} entries")
            cleaned = [_required(str(item), maximum=300, label="Each Drive file name") for item in names]
            maximum = _bounded_int(max_results, minimum=1, maximum=100, label="max_results")
            clauses = " or ".join(f"name = '{_drive_literal(item)}'" for item in cleaned)
            query = f"trashed = false and ({clauses})"
            if folder_id.strip():
                folder = _required(folder_id, maximum=200, label="folder_id")
                query += f" and '{_drive_literal(folder)}' in parents"
            response = await service.get("/drive/v3/files", params={
                "q": query,
                "pageSize": maximum,
                "orderBy": "modifiedTime desc",
                "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
                "spaces": "drive",
            })
            files = _json(response).get("files", [])
            return {"files": [item for item in files if isinstance(item, dict)][:maximum]}

        @server.tool()
        async def read_file_content(file_id: str, max_characters: int = 6000) -> dict:
            """Read text from a Drive document or textual file without changing it."""
            identifier = _required(file_id, maximum=200, label="file_id")
            limit = _bounded_int(max_characters, minimum=1, maximum=100_000, label="max_characters")
            encoded = quote(identifier, safe="")
            metadata = _json(await service.get(
                f"/drive/v3/files/{encoded}", params={"fields": "id,name,mimeType,modifiedTime,webViewLink"}
            ))
            mime = str(metadata.get("mimeType", ""))
            if mime == DRIVE_DOC:
                response = await service.get(f"/drive/v3/files/{encoded}/export", params={"mimeType": "text/plain"})
            elif mime == DRIVE_SHEET:
                response = await service.get(f"/drive/v3/files/{encoded}/export", params={"mimeType": "text/csv"})
            elif mime == DRIVE_SLIDES:
                return {"file": metadata, "text": "", "warning": "Google Slides text export is not supported"}
            elif mime.startswith("text/") or mime in {"application/json", "application/xml", "text/csv"}:
                response = await service.get(f"/drive/v3/files/{encoded}", params={"alt": "media"})
            else:
                return {"file": metadata, "text": "", "warning": "This file type is not readable as text"}
            text = response.content.decode("utf-8", errors="replace")
            result = {"file": metadata, "text": text[:limit]}
            if len(text) > limit:
                result["warning"] = "Content was truncated to max_characters"
            return result

    elif name == "gmail":
        @server.tool()
        async def search_feedback(query: str, max_results: int = 5) -> dict:
            """Search the authorized mailbox and return bounded message text; attachments are ignored."""
            search = _required(query, maximum=2000, label="query")
            maximum = _bounded_int(max_results, minimum=1, maximum=25, label="max_results")
            listing = _json(await service.get("/gmail/v1/users/me/messages", params={
                "q": search, "maxResults": maximum,
            }))
            messages = []
            for row in listing.get("messages", [])[:maximum]:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                identifier = quote(str(row["id"]), safe="")
                item = _json(await service.get(
                    f"/gmail/v1/users/me/messages/{identifier}", params={"format": "full"}
                ))
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                messages.append({
                    "id": str(item.get("id", "")),
                    "thread_id": str(item.get("threadId", "")),
                    "subject": _header(payload.get("headers"), "Subject"),
                    "text": _mime_text(payload),
                    "internal_date": str(item.get("internalDate", "")),
                })
            return {"messages": messages}

    else:
        @server.tool()
        async def upcoming_events(time_min: str, time_max: str, query: str = "",
                                  calendar_id: str = "primary", max_results: int = 20) -> dict:
            """List upcoming non-cancelled events in one authorized calendar."""
            start = _required(time_min, maximum=80, label="time_min")
            end = _required(time_max, maximum=80, label="time_max")
            calendar = _required(calendar_id, maximum=500, label="calendar_id")
            maximum = _bounded_int(max_results, minimum=1, maximum=100, label="max_results")
            response = await service.get(
                f"/calendar/v3/calendars/{quote(calendar, safe='')}/events",
                params={"timeMin": start, "timeMax": end, "q": query.strip()[:500],
                        "singleEvents": "true", "orderBy": "startTime", "maxResults": maximum},
            )
            events = []
            for item in _json(response).get("items", []):
                if not isinstance(item, dict) or item.get("status") == "cancelled":
                    continue
                events.append({key: item.get(key) for key in
                               ("id", "summary", "start", "end", "status", "htmlLink", "updated")})
            return {"events": events}

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Newsletter Google read-only MCP server")
    parser.add_argument("service", choices=("google_drive", "gmail", "google_calendar"))
    args = parser.parse_args()
    asyncio.run(server_for(args.service).serve())


if __name__ == "__main__":
    main()
