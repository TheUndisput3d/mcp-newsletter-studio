"""Offline fixture MCP used only by ``newsletter demo`` and tests.

It contains no provider API code.  Production always launches the external
MCP connection declared in config.yaml.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from newsletter_mcp import demo
from newsletter_mcp.protocol import ToolFailure, ToolServer


def workspace_path(path: str) -> Path:
    root = Path(os.environ.get("NEWSLETTER_WORKSPACE", "workspace")).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or path.startswith("."):
        raise ToolFailure("Invalid demo workspace path")
    return target


def server_for(name: str) -> ToolServer:
    server = ToolServer("synthetic-" + name)

    @server.tool()
    def status() -> dict:
        return {"ready": True, "synthetic": True}

    if name == "filesystem":
        @server.tool()
        def read_text(path: str) -> dict:
            return {"text": workspace_path(path).read_text(encoding="utf-8")}

        @server.tool(read_only=False)
        def write_text(path: str, text: str, overwrite: bool = False) -> dict:
            target = workspace_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                raise ToolFailure("File exists")
            target.write_text(text, encoding="utf-8")
            return {"path": path}
    elif name == "tavily":
        @server.tool()
        def search(query: str, topic: str = "news", search_depth: str = "basic", start_date: str = "", end_date: str = "", max_results: int = 6, include_domains: list[str] | None = None) -> dict:
            return demo.records("web")
    elif name in {"arxiv", "github", "producthunt", "twitter"}:
        channel = name
        @server.tool()
        def search_papers(query: str, start_time: str = "", end_time: str = "", max_results: int = 6) -> dict:
            return demo.records(channel)
        @server.tool()
        def search_repositories(query: str, start_time: str = "", end_time: str = "", max_results: int = 6) -> dict:
            return demo.records(channel)
        @server.tool()
        def search_products(topic: str = "", start_time: str = "", end_time: str = "", max_results: int = 6) -> dict:
            return demo.records(channel)
        @server.tool()
        def search_posts(query: str, start_time: str = "", end_time: str = "", max_results: int = 6) -> dict:
            return demo.records(channel)
    elif name == "google_drive":
        @server.tool()
        def find_files(names: list[str], folder_id: str = "", max_results: int = 20) -> dict:
            return demo.context(name)
        @server.tool()
        def read_file(file_id: str, **_kwargs) -> dict:
            return {"text": "Synthetic local context for " + file_id}
    elif name == "gmail":
        @server.tool()
        def search_feedback(query: str, max_results: int = 5) -> dict:
            return demo.context(name)
    else:
        @server.tool()
        def upcoming_events(time_min: str, time_max: str, query: str = "", calendar_id: str = "primary") -> dict:
            return demo.context(name)
    return server


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m newsletter_mcp.synthetic_mcp SERVER")
    asyncio.run(server_for(sys.argv[1]).serve())


if __name__ == "__main__":
    main()
