import base64

import httpx
import pytest

from newsletter_mcp.config import MCPConfig
from newsletter_mcp.google_mcp import GoogleAPI, server_for
from newsletter_mcp.protocol import ToolFailure
from newsletter_mcp.registry import MCPHub


def encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def api_with(handler) -> GoogleAPI:
    return GoogleAPI("short-lived-token", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def call(api: GoogleAPI, service: str, tool: str, **arguments):
    function = server_for(service, api=api).tools[tool].function
    return await function(**arguments)


async def test_drive_search_escapes_query_and_reads_document():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer short-lived-token"
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [{"id": "1", "name": "Editor's Notes"}]})
        if request.url.path == "/drive/v3/files/1":
            return httpx.Response(200, json={"id": "1", "name": "Notes", "mimeType":
                "application/vnd.google-apps.document"})
        if request.url.path == "/drive/v3/files/1/export":
            return httpx.Response(200, content=b"abcdef")
        raise AssertionError(request.url)

    api = api_with(handler)
    found = await call(api, "google_drive", "search_files", names=["Editor's Notes"], folder_id="folder")
    document = await call(api, "google_drive", "read_file_content", file_id="1", max_characters=4)
    assert found["files"][0]["id"] == "1"
    query = requests[0].url.params["q"]
    assert "Editor's Notes" not in query
    assert "Editor\\'s Notes" in query
    assert "'folder' in parents" in query
    assert document["text"] == "abcd"
    assert "truncated" in document["warning"]


async def test_gmail_extracts_plain_text_and_skips_attachments():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"] == "newsletter feedback"
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={
            "id": "m1", "threadId": "t1", "internalDate": "1",
            "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "Subject", "value": "Useful"}],
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": encoded("Reader feedback")}},
                            {"mimeType": "text/plain", "filename": "secret.txt",
                             "body": {"attachmentId": "a1", "data": encoded("must not appear")}},
                        ]},
        })

    result = await call(api_with(handler), "gmail", "search_feedback",
                        query="newsletter feedback", max_results=5)
    assert result["messages"] == [{"id": "m1", "thread_id": "t1", "subject": "Useful",
                                   "text": "Reader feedback", "internal_date": "1"}]


async def test_gmail_sanitizes_html_fallback():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        html = "<p>Keep this</p><script>remove-me()</script>"
        return httpx.Response(200, json={"id": "m1", "payload": {
            "headers": [], "mimeType": "text/html", "body": {"data": encoded(html)}}})

    result = await call(api_with(handler), "gmail", "search_feedback", query="feedback", max_results=1)
    assert result["messages"][0]["text"] == "Keep this"


async def test_calendar_encodes_calendar_and_filters_cancelled_events():
    def handler(request: httpx.Request):
        assert request.url.raw_path.split(b"?", 1)[0] == b"/calendar/v3/calendars/team%40example.com/events"
        assert request.url.params["singleEvents"] == "true"
        assert request.url.params["orderBy"] == "startTime"
        return httpx.Response(200, json={"items": [
            {"id": "gone", "status": "cancelled"},
            {"id": "next", "summary": "AI Newsletter", "status": "confirmed",
             "start": {"dateTime": "2026-11-12T08:00:00Z"}, "end": {"dateTime": "2026-11-12T09:00:00Z"}},
        ]})

    result = await call(api_with(handler), "google_calendar", "upcoming_events",
                        time_min="2026-11-10T00:00:00Z", time_max="2026-12-10T00:00:00Z",
                        query="AI Newsletter", calendar_id="team@example.com", max_results=20)
    assert [event["id"] for event in result["events"]] == ["next"]


async def test_google_error_does_not_expose_provider_body_or_token():
    secret_body = "private-provider-detail short-lived-token"

    def handler(request: httpx.Request):
        return httpx.Response(403, content=secret_body)

    with pytest.raises(ToolFailure) as error:
        await call(api_with(handler), "google_drive", "search_files", names=["Notes"])
    assert "access denied" in str(error.value)
    assert secret_body not in str(error.value)
    assert "short-lived-token" not in str(error.value)


async def test_server_status_and_tools_are_read_only():
    server = server_for("google_drive", api=api_with(lambda request: httpx.Response(500)))
    assert await server.tools["status"].function() == {
        "ready": True, "service": "google_drive", "read_only": True,
    }
    assert set(server.tools) == {"status", "search_files", "read_file_content"}
    assert all(tool.descriptor["annotations"]["readOnlyHint"] for tool in server.tools.values())


async def test_hub_injects_google_oauth_only_into_google_subprocess(settings, monkeypatch):
    captured = []

    class Client:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def list_tools(self):
            return [{"name": "status"}]

        async def call(self, name, arguments=None):
            return {"ready": True}

    async def token(*args):
        return "refreshed-google-token"

    settings.mcps["google_drive"] = MCPConfig(
        enabled=True, transport="stdio", authentication="oauth", token_env="GOOGLE_ACCESS_TOKEN",
        command="python", args=["-m", "newsletter_mcp.google_mcp", "google_drive"],
        operations={"find_files": "search_files", "read_file": "read_file_content"},
    )
    monkeypatch.setattr("newsletter_mcp.registry.MCPClient", Client)
    monkeypatch.setattr("newsletter_mcp.mcp_oauth.access_token", token)
    async with MCPHub(settings):
        pass
    google = next(item for item in captured if "newsletter_mcp.google_mcp" in item["command"])
    filesystem = next(item for item in captured if "server-filesystem" in " ".join(item["command"]))
    assert google["env"]["GOOGLE_ACCESS_TOKEN"] == "refreshed-google-token"
    assert "GOOGLE_ACCESS_TOKEN" not in filesystem["env"]
