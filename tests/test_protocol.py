import json

import httpx
import pytest

from newsletter_mcp.protocol import MCPClient, MCPError, ToolFailure, ToolServer
from newsletter_mcp.registry import MCPHub


async def test_server_lifecycle_validation_and_errors():
    server = ToolServer("test")
    @server.tool()
    def add(a: int, b: int = 2) -> dict:
        return {"sum": a + b}
    pre = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert pre["error"]["code"] == -32002
    init = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
    assert init["result"]["protocolVersion"] == "2025-11-25"
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = await server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert tools["result"]["tools"][0]["inputSchema"]["required"] == ["a"]
    result = await server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 3}}})
    assert result["result"]["structuredContent"]["sum"] == 5
    bad = await server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "add", "arguments": {"secret": "not allowed"}}})
    assert bad["result"]["isError"]
    assert "secret" not in json.dumps(bad)
    unknown = await server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "missing"}})
    assert unknown["error"]["code"] == -32602


async def test_all_nine_synthetic_demo_mcp_servers(settings):
    async with MCPHub(settings, demo=True) as hub:
        assert len(hub.clients) == 9
        assert all(status["state"] == "demo" for status in hub.status.values())
        assert "search_posts" in hub.status["twitter"]["tools"]
        await hub.write("test/check.txt", "MCP round trip\n")
        assert await hub.read("test/check.txt") == "MCP round trip\n"
        with pytest.raises(ToolFailure):
            await hub.write("../escape.txt", "blocked")
        result = await hub.search_web("test", "2026-11-01", "2026-11-08", 6)
        assert len(result["items"]) == 5
    assert all(client.closed for client in hub.clients.values())


async def test_remote_json_and_sse():
    calls = []
    async def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "session-1"}, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}})
        assert request.headers["Mcp-Session-Id"] == "session-1"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text='event: message\ndata: ' + json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}}) + '\n\n')
    client = MCPClient(url="https://example.com/mcp", token="fake-token")
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = await client.request("initialize", {})
    assert first["protocolVersion"] == "2025-11-25"
    assert await client.list_tools() == []
    assert "text/event-stream" in calls[0].headers["Accept"]
    await client.http.aclose()


async def test_mismatched_response_is_rejected():
    with pytest.raises(MCPError):
        MCPClient._unwrap({"jsonrpc": "2.0", "id": 2, "result": {}}, 1)


def test_x_protocol_version_can_be_pinned_to_official_contract():
    client = MCPClient(url="https://api.x.com/mcp", protocol_version="2025-06-18")
    assert client.version == "2025-06-18"
    with pytest.raises(ValueError, match="Unsupported"):
        MCPClient(url="https://api.x.com/mcp", protocol_version="invented")
