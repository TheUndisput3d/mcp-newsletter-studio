"""Small tools-only MCP implementation.

Real JSON-RPC initialize/initialized, tools/list, tools/call and ping over stdio;
client-side Streamable HTTP also accepts JSON or SSE responses. No sampling,
resources, prompts, OAuth discovery, task extension or legacy HTTP+SSE advertised.
See docs/ARCHITECTURE.md for the deliberately bounded interoperability surface.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

import httpx
from pydantic import ConfigDict, ValidationError, create_model

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class MCPError(RuntimeError):
    pass


class ToolFailure(MCPError):
    """Safe-to-display, credential-free error from a tool."""


@dataclass
class RegisteredTool:
    function: Callable
    validator: type
    descriptor: dict


class ToolServer:
    def __init__(self, name: str):
        self.name = name
        self.tools: dict[str, RegisteredTool] = {}
        self.initialized = False
        self.ready = False
        self.version = VERSIONS[0]

    def tool(self, *, read_only: bool = True):
        def decorate(function: Callable):
            fields = {}
            hints = get_type_hints(function)
            for name, param in inspect.signature(function).parameters.items():
                default = ... if param.default is inspect.Parameter.empty else param.default
                fields[name] = (hints.get(name, Any), default)
            validator = create_model(function.__name__ + "Arguments", __config__=ConfigDict(extra="forbid"),
                                     **fields)
            descriptor = {
                "name": function.__name__,
                "description": inspect.getdoc(function) or function.__name__,
                "inputSchema": validator.model_json_schema(),
                "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                                "idempotentHint": read_only, "openWorldHint": self.name != "filesystem"},
            }
            self.tools[function.__name__] = RegisteredTool(function, validator, descriptor)
            return function
        return decorate

    async def handle(self, message: dict) -> dict | None:
        identifier = message.get("id") if isinstance(message, dict) else None
        def error(code: int, text: str):
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": text}}
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return error(-32600, "Invalid JSON-RPC request")
        method = message["method"]
        if "id" not in message:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        params = message.get("params", {})
        if not isinstance(params, dict):
            return error(-32602, "Parameters must be an object")
        if method == "initialize":
            if self.initialized:
                return error(-32600, "Session already initialized")
            self.version = params.get("protocolVersion") if params.get("protocolVersion") in VERSIONS else VERSIONS[0]
            self.initialized = True
            result = {"protocolVersion": self.version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "newsletter-" + self.name, "version": "2.0.0"}}
        elif method == "ping":
            result = {}
        elif not self.ready:
            return error(-32002, "Initialize the session and send notifications/initialized first")
        elif method == "tools/list":
            result = {"tools": [t.descriptor for t in self.tools.values()]}
        elif method == "tools/call":
            tool = self.tools.get(params.get("name", ""))
            if tool is None:
                return error(-32602, "Unknown tool")
            try:
                args = tool.validator.model_validate(params.get("arguments", {})).model_dump()
                value = tool.function(**args)
                if inspect.isawaitable(value):
                    value = await value
                # A text JSON representation is provided for older clients as well.
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                          "isError": False}
                if self.version in VERSIONS[:2] and isinstance(value, dict):
                    result["structuredContent"] = value
            except ValidationError:
                result = {"content": [{"type": "text", "text": "Invalid tool arguments; inspect tools/list schema"}],
                          "isError": True}
            except ToolFailure as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except Exception as exc:
                # Do not leak provider response bodies, private content or tokens.
                result = {"content": [{"type": "text", "text": f"Tool failed ({type(exc).__name__})"}],
                          "isError": True}
        else:
            return error(-32601, "Method not found")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    async def serve(self) -> None:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_MESSAGE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE_BYTES:
                return  # An untrusted peer cannot force unbounded allocation.
            try:
                response = await self.handle(json.loads(line))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}}
            if response is not None:
                encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
                if len(encoded) > MAX_MESSAGE_BYTES:
                    encoded = json.dumps({"jsonrpc": "2.0", "id": response.get("id"),
                                          "error": {"code": -32603, "message": "Response exceeds size limit"}}).encode() + b"\n"
                sys.stdout.buffer.write(encoded)
                sys.stdout.buffer.flush()


class MCPClient:
    """One sequential request at a time per server, with deterministic cleanup."""
    def __init__(self, *, command: list[str] | None = None, env: dict | None = None,
                 cwd: str | None = None, url: str | None = None, token: str = "", timeout: float = 90,
                 protocol_version: str | None = None):
        self.command, self.env, self.cwd = command, env, cwd
        self.url, self.token, self.timeout = url, token, timeout
        self.process: asyncio.subprocess.Process | None = None
        self.http: httpx.AsyncClient | None = None
        self.session_id: str | None = None
        if protocol_version is not None and protocol_version not in VERSIONS:
            raise ValueError("Unsupported requested MCP protocol version")
        self.version = protocol_version or VERSIONS[0]
        self.server_info: dict[str, Any] = {}
        self.counter = 0
        self.lock = asyncio.Lock()
        self._stderr_task: asyncio.Task | None = None
        self.closed = False

    async def __aenter__(self):
        try:
            if self.command:
                self.process = await asyncio.create_subprocess_exec(
                    *self.command, env=self.env, cwd=self.cwd,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE_BYTES + 1)
                self._stderr_task = asyncio.create_task(self._drain_stderr())
            else:
                if not self.url or not self.url.startswith("https://"):
                    raise MCPError("Remote MCP URL must use HTTPS")
                self.http = httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
            result = await self.request("initialize", {"protocolVersion": self.version, "capabilities": {},
                                         "clientInfo": {"name": "newsletter-studio", "version": "2.0.0"}})
            if result.get("protocolVersion") not in VERSIONS:
                raise MCPError("Server negotiated an unsupported MCP version")
            self.version = result["protocolVersion"]
            self.server_info = result.get("serverInfo", {}) if isinstance(result.get("serverInfo"), dict) else {}
            await self.notify("notifications/initialized")
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *args):
        await self.close()

    async def _drain_stderr(self):
        # Drain to avoid deadlocks, but don't retain potentially sensitive provider logs.
        if self.process and self.process.stderr:
            while await self.process.stderr.read(8192):
                pass

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": self.version}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    @staticmethod
    def _unwrap(message: dict, identifier: int) -> dict:
        if message.get("id") != identifier or message.get("jsonrpc") != "2.0":
            raise MCPError("Mismatched JSON-RPC response")
        if "error" in message:
            raise MCPError(f"MCP protocol error {message['error'].get('code', 'unknown')}")
        if "result" not in message or not isinstance(message["result"], dict):
            raise MCPError("Missing MCP result")
        return message["result"]

    async def _remote(self, payload: dict, expects_response: bool) -> dict:
        assert self.http is not None
        async with self.http.stream("POST", self.url, json=payload, headers=self._headers()) as response:
            if response.status_code >= 400:
                raise MCPError(f"Remote MCP returned HTTP {response.status_code}")
            if response.headers.get("Mcp-Session-Id"):
                self.session_id = response.headers["Mcp-Session-Id"]
            if not expects_response:
                await response.aread()
                return {}
            if "text/event-stream" not in response.headers.get("content-type", ""):
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_MESSAGE_BYTES:
                        raise MCPError("Remote MCP response too large")
                return self._unwrap(json.loads(body), payload["id"])
            data: list[str] = []
            count = 0
            async for line in response.aiter_lines():
                count += len(line)
                if count > MAX_MESSAGE_BYTES:
                    raise MCPError("Remote MCP event stream too large")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    text = "\n".join(data)
                    data = []
                    if not text.strip():
                        continue
                    message = json.loads(text)
                    if message.get("id") == payload["id"] and ("result" in message or "error" in message):
                        return self._unwrap(message, payload["id"])
                    if "method" in message and "id" in message:
                        raise MCPError("Remote server requested an unadvertised client capability")
            raise MCPError("Remote MCP closed before returning the tool result")

    async def request(self, method: str, params: dict | None = None) -> dict:
        async with self.lock:
            if self.closed:
                raise MCPError("MCP connection is closed")
            self.counter += 1
            payload = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params or {}}
            async def perform():
                if self.http:
                    return await self._remote(payload, True)
                assert self.process and self.process.stdin and self.process.stdout
                encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
                if len(encoded) > MAX_MESSAGE_BYTES:
                    raise MCPError("MCP request too large")
                self.process.stdin.write(encoded)
                await self.process.stdin.drain()
                while True:
                    line = await self.process.stdout.readline()
                    if not line:
                        raise MCPError("MCP server exited without a response")
                    message = json.loads(line)
                    if "id" not in message:
                        continue
                    return self._unwrap(message, payload["id"])
            try:
                return await asyncio.wait_for(perform(), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                await self.close()  # Never reuse a session with an in-flight stale response.
                raise MCPError(f"MCP request timed out after {self.timeout:g} seconds") from exc

    async def notify(self, method: str):
        payload = {"jsonrpc": "2.0", "method": method}
        if self.http:
            await asyncio.wait_for(self._remote(payload, False), self.timeout)
        else:
            assert self.process and self.process.stdin
            self.process.stdin.write((json.dumps(payload) + "\n").encode())
            await self.process.stdin.drain()

    async def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise MCPError("Tool discovery pagination limit exceeded")

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            # Bundled servers only emit safe errors. Remote errors are summarized.
            raise ToolFailure(text[:500] if self.process else "Remote MCP tool returned an error")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        # Standard filesystem-style MCP tools return a plain text content block.
        # Preserve it without treating it as a provider-specific API response.
        return {"text": text}

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.http:
            if self.session_id:
                try:
                    await self.http.delete(self.url, headers=self._headers(), timeout=5)
                except Exception:
                    pass
            await self.http.aclose()
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        if self._stderr_task:
            await self._stderr_task
