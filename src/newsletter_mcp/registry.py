from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

from newsletter_mcp.config import SERVER_NAMES, Settings
from newsletter_mcp.protocol import MCPClient, MCPError

def external_environment(settings: Settings, values: dict[str, str]) -> dict[str, str]:
    """Pass only the explicitly declared environment to an external server."""
    inherited = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
                 "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "PYTHONPATH")
    env = {k: os.environ[k] for k in inherited if k in os.environ}
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    for target, value in values.items():
        resolved = value.replace("${workspace}", str(settings.workspace))
        if resolved.startswith("${") and resolved.endswith("}") and resolved.count("${") == 1:
            resolved = os.getenv(resolved[2:-1], "")
        if resolved:
            env[target] = resolved
    return env


class MCPHub:
    def __init__(self, settings: Settings, *, demo: bool = False):
        self.settings, self.demo = settings, demo
        self.stack = AsyncExitStack()
        self.clients: dict[str, MCPClient] = {}
        self.tool_descriptors: dict[str, dict[str, dict]] = {}
        self.status: dict[str, dict] = {}
        self.audit: list[dict] = []

    async def __aenter__(self):
        await self.stack.__aenter__()
        try:
            for name in SERVER_NAMES:
                config = self.settings.mcps[name]
                if not config.enabled and not self.demo:
                    self.status[name] = {"state": "disabled", "detail": "Disabled in config.yaml"}
                    continue
                try:
                    if self.demo:
                        # Synthetic data is test-only and does not implement any provider API.
                        client = MCPClient(command=[sys.executable, "-m", "newsletter_mcp.synthetic_mcp", name],
                                           env=external_environment(self.settings, {"NEWSLETTER_WORKSPACE": "${workspace}"}),
                                           cwd=str(self.settings.project_root), timeout=config.timeout_seconds)
                    else:
                        token = ""
                        if config.authentication == "oauth":
                            from newsletter_mcp.mcp_oauth import access_token
                            try:
                                token = os.getenv(config.token_env, "") or await access_token(self.settings, name)
                            except MCPError as exc:
                                self.status[name] = {"state": "unconfigured", "detail": str(exc)}
                                continue
                        elif config.authentication == "bearer":
                            token = os.getenv(config.token_env, "") if config.authentication == "bearer" else ""
                        if config.authentication in {"bearer", "oauth"} and not token:
                            self.status[name] = {
                                "state": "unconfigured",
                                "detail": f"Set {config.token_env} in Settings > Credentials",
                            }
                            continue
                        if config.transport == "streamable_http":
                            protocol_version = "2025-06-18" if name == "twitter" else None
                            client = MCPClient(url=config.url, token=token, timeout=config.timeout_seconds,
                                               protocol_version=protocol_version)
                        else:
                            command_args = [arg.replace("${workspace}", str(self.settings.workspace)) for arg in config.args]
                            declared_environment = dict(config.environment)
                            if token and config.token_env:
                                # Give this subprocess only its own short-lived credential.
                                declared_environment[config.token_env] = token
                            client = MCPClient(command=[config.command, *command_args],
                                               env=external_environment(self.settings, declared_environment),
                                               cwd=str(self.settings.project_root), timeout=config.timeout_seconds)
                    connection = await self.stack.enter_async_context(client)
                    tools = await connection.list_tools()
                    names = [t["name"] for t in tools]
                    descriptors = {t["name"]: t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)}
                    if not self.demo and name == "twitter":
                        validate_official_x_contract(config.url, config.operations, connection.server_info, descriptors)
                    health = await connection.call("status") if "status" in names else {"ready": True}
                    self.clients[name] = connection
                    self.tool_descriptors[name] = descriptors
                    self.status[name] = {"state": "demo" if self.demo else "ready" if health.get("ready") else "unconfigured",
                                         "tools": names, "detail": health}
                except Exception as exc:
                    self.status[name] = {"state": "unavailable", "detail": str(exc)[:300]}
            if "filesystem" not in self.clients:
                raise MCPError("Filesystem MCP is unavailable; newsletter artifacts cannot be read or written")
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        await self.stack.aclose()

    def ready(self, name: str) -> bool:
        return self.status.get(name, {}).get("state") in {"ready", "demo"}

    async def call(self, server: str, tool: str, args: dict | None = None) -> dict:
        if not self.ready(server):
            raise MCPError(f"{server} MCP is {self.status.get(server, {}).get('state', 'unavailable')}")
        event = {"server": server, "tool": tool}
        # Argument values can contain private data. Audit tool names and key names only.
        event["argument_keys"] = sorted((args or {}).keys())
        self.audit.append(event)
        try:
            actual_tool = tool if self.demo else self.settings.mcps[server].operations.get(tool, tool)
            outbound = dict(args or {})
            if not self.demo and server == "arxiv" and tool == "search_papers":
                start, end = outbound.pop("start_time", ""), outbound.pop("end_time", "")
                if start:
                    outbound["submitted_from"] = start[:10]
                if end:
                    outbound["submitted_to"] = end[:10]
                outbound.update(sort_by="submitted", sort_order="descending")
            if not self.demo and server == "github" and tool == "search_repositories":
                start, end = outbound.pop("start_time", ""), outbound.pop("end_time", "")
                limit = outbound.pop("max_results", 6)
                if start and end and "pushed:" not in outbound.get("query", ""):
                    outbound["query"] = f"{outbound.get('query', '')} pushed:{start[:10]}..{end[:10]}"
                outbound.update(perPage=limit, sort="stars", order="desc", minimal_output=False)
            if not self.demo and server == "producthunt" and tool == "search_products":
                start, end = outbound.pop("start_time", ""), outbound.pop("end_time", "")
                limit = max(1, min(int(outbound.pop("max_results", 6)), 8))
                topic = str(outbound.pop("topic", "")).strip()
                days_ago = 7
                if start and end:
                    try:
                        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
                        end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
                        days_ago = max(1, min(365, (end_at.date() - start_at.date()).days or 1))
                    except ValueError:
                        pass
                outbound = {"days_ago": days_ago, "limit": limit}
                if topic:
                    outbound["query"] = topic
            if not self.demo and server == "twitter" and tool == "search_posts":
                query = str(outbound.get("query", "")).strip()
                if not query:
                    raise MCPError("X post search requires a nonempty query")
                limit = max(10, min(int(outbound.get("max_results", 10)), 500))

                def x_time(value: object, *, latest: bool = False) -> str:
                    if not isinstance(value, str) or not value:
                        return ""
                    try:
                        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise MCPError("X post search received an invalid date-time") from exc
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    moment = moment.astimezone(timezone.utc)
                    # X search does not index the final few seconds immediately.
                    if latest:
                        moment -= timedelta(seconds=15)
                    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

                outbound = {
                    "query": query,
                    "max_results": limit,
                    "sort_order": "recency",
                    # author_id is supplied by the author_id expansion; it is not
                    # an allowed post.fields value in X's current OpenAPI schema.
                    "post.fields": "created_at,public_metrics,lang",
                    "expansions": "author_id",
                    "user.fields": "username,name,verified,public_metrics",
                }
                start_time = x_time((args or {}).get("start_time"))
                end_time = x_time((args or {}).get("end_time"), latest=True)
                if start_time:
                    outbound["start_time"] = start_time
                if end_time:
                    outbound["end_time"] = end_time
            # The standard filesystem MCP calls file contents "content".
            if not self.demo and server == "filesystem" and tool == "write_text":
                outbound["content"] = outbound.pop("text", "")
                outbound.pop("overwrite", None)
            if not self.demo and server == "filesystem" and isinstance(outbound.get("path"), str):
                target = (self.settings.workspace / outbound["path"]).resolve()
                if not target.is_relative_to(self.settings.workspace):
                    raise MCPError("Filesystem path escapes the configured workspace")
                outbound["path"] = str(target)
                if tool == "write_text":
                    create = self.settings.mcps[server].operations.get("create_directory", "create_directory")
                    cursor = self.settings.workspace
                    for part in target.parent.relative_to(self.settings.workspace).parts:
                        cursor = cursor / part
                        await self.clients[server].call(create, {"path": str(cursor)})
            result = await self.clients[server].call(actual_tool, outbound)
            if not self.demo and server == "filesystem" and tool == "read_text" and "text" not in result and isinstance(result.get("content"), str):
                result = {"text": result["content"]}
            if not self.demo and server == "arxiv" and tool == "search_papers":
                result = normalize_arxiv_result(result)
            if not self.demo and server == "github" and tool == "search_repositories":
                result = normalize_github_result(result)
            if not self.demo and server == "producthunt" and tool == "search_products":
                result = normalize_producthunt_result(result)
            if not self.demo and server == "twitter" and tool == "search_posts":
                result = normalize_x_result(result)
            event["outcome"] = "ok"
            return result
        except Exception:
            event["outcome"] = "error"
            raise

    async def search_web(self, query: str, start_date: str, end_date: str, max_results: int, *, topic: str = "news", include_domains: list[str] | None = None) -> dict:
        result = await self.call("tavily", "search", {"query": query, "topic": topic,
            "search_depth": "basic", "start_date": start_date, "end_date": end_date,
            "max_results": max_results, "include_domains": include_domains or []})
        return normalize_web_result(result)

    async def write(self, path: str, text: str, *, overwrite: bool = False):
        return await self.call("filesystem", "write_text", {"path": path, "text": text, "overwrite": overwrite})

    async def read(self, path: str) -> str:
        result = await self.call("filesystem", "read_text", {"path": path})
        return result["text"]


def normalize_web_result(result: dict[str, Any]) -> dict:
    """Normalize the official Tavily MCP's documented results shape, not an API adapter."""
    rows = result.get("results", result.get("items", []))
    if not isinstance(rows, list):
        raise MCPError("Tavily MCP returned no result list")
    items = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("url"):
            continue
        published = row.get("published_date") or row.get("published_at")
        if isinstance(published, str) and "," in published:
            try:
                from email.utils import parsedate_to_datetime
                published = parsedate_to_datetime(published).isoformat()
            except (TypeError, ValueError):
                published = None
        items.append({"title": row.get("title") or row["url"], "url": row["url"],
                      "summary": row.get("content") or row.get("snippet") or "",
                      "published_at": published,
                      "metadata": {k: v for k, v in row.items() if k not in {"title", "url", "content", "snippet", "published_date", "published_at"}}})
    return {"items": items}


def normalize_arxiv_result(result: dict[str, Any]) -> dict:
    items = []
    for paper in result.get("papers", []):
        if not isinstance(paper, dict) or not paper.get("abstract_url"):
            continue
        items.append({"title": paper.get("title") or paper.get("id", "arXiv paper"),
                      "url": paper["abstract_url"], "summary": paper.get("abstract", ""),
                      "published_at": paper.get("published"),
                      "metadata": {"authors": paper.get("authors", []),
                                   "categories": paper.get("categories", []),
                                   "primary_category": paper.get("primary_category"),
                                   "pdf_url": paper.get("pdf_url"),
                                   "evidence_level": "abstract"}})
    return {"items": items}


def normalize_github_result(result: dict[str, Any]) -> dict:
    items = []
    for repo in result.get("items", []):
        if not isinstance(repo, dict) or not repo.get("html_url"):
            continue
        items.append({"title": repo.get("full_name") or repo.get("name") or "GitHub repository",
                      "url": repo["html_url"], "summary": repo.get("description") or "",
                      "published_at": repo.get("pushed_at") or repo.get("updated_at"),
                      "metadata": {"stars": repo.get("stargazers_count", 0),
                                   "language": repo.get("language"), "topics": repo.get("topics", []),
                                   "date_kind": "last_push", "forks": repo.get("forks_count", 0)}})
    return {"items": items}


def normalize_producthunt_result(result: dict[str, Any]) -> dict:
    """Normalize the official Product Hunt MCP search_launches response."""
    data = result.get("data", {})
    rows = data.get("products", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        raise MCPError("Product Hunt MCP returned no product list")
    items = []
    for product in rows:
        if not isinstance(product, dict) or not product.get("url"):
            continue
        description = product.get("description") or product.get("tagline") or ""
        items.append({
            "title": product.get("name") or product["url"],
            "url": product["url"],
            "summary": description,
            "published_at": product.get("launched_at"),
            "metadata": {
                "tagline": product.get("tagline"),
                "slug": product.get("slug"),
                "categories": product.get("categories", []),
                "makers": product.get("makers", []),
                "votes": product.get("votes_count", 0),
                "reviews_count": product.get("reviews_count", 0),
                "reviews_rating": product.get("reviews_rating"),
                "pricing_type": product.get("pricing_type"),
                "retrieval_method": "producthunt_official_mcp",
                "evidence_level": "authenticated_platform_data",
            },
        })
    return {"items": items}


def validate_official_x_contract(url: str, operations: dict[str, str], server_info: dict[str, Any],
                                 tools: dict[str, dict]) -> None:
    """Fail closed if the configured X connection is not X's hosted MCP contract."""
    if url != "https://api.x.com/mcp":
        raise MCPError("X research must use the official hosted MCP endpoint")
    if server_info.get("name") != "xmcp":
        raise MCPError("X MCP server identity did not match the official hosted service")
    tool_name = operations.get("search_posts")
    if tool_name != "search_posts_all":
        raise MCPError("X MCP must map post research to the official search_posts_all tool")
    descriptor = tools.get(tool_name)
    if descriptor is None:
        raise MCPError("Official X MCP did not advertise search_posts_all")
    schema = descriptor.get("inputSchema", {})
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    expected_types = {
        "query": "string", "start_time": "string", "end_time": "string",
        "max_results": "integer", "post.fields": "string",
        "expansions": "string", "user.fields": "string",
    }
    if "query" not in required or any(
        not isinstance(properties.get(key), dict) or properties[key].get("type") != expected
        for key, expected in expected_types.items()
    ):
        raise MCPError("Official X MCP search_posts_all schema is incompatible")


def normalize_x_result(result: dict[str, Any]) -> dict:
    """Normalize the official X search_posts_all response into newsletter sources."""
    rows = result.get("data", [])
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise MCPError("Official X MCP returned an invalid post list")
    includes = result.get("includes", {})
    users = includes.get("users", []) if isinstance(includes, dict) else []
    author_by_id = {
        str(user.get("id")): user
        for user in users
        if isinstance(user, dict) and user.get("id")
    }
    items = []
    for post in rows:
        if not isinstance(post, dict) or not post.get("id") or not post.get("text"):
            continue
        post_id = str(post["id"])
        author_id = str(post.get("author_id", ""))
        author = author_by_id.get(author_id, {})
        username = str(author.get("username", "")).strip()
        url = f"https://x.com/{username}/status/{post_id}" if username else f"https://x.com/i/web/status/{post_id}"
        text = str(post["text"]).strip()
        byline = "@" + username if username else "X post"
        items.append({
            "title": f"{byline}: {text[:140]}",
            "url": url,
            "summary": text,
            "published_at": post.get("created_at"),
            "metadata": {
                "post_id": post_id,
                "author_id": author_id or None,
                "author_username": username or None,
                "author_name": author.get("name"),
                "author_verified": author.get("verified"),
                "language": post.get("lang"),
                "public_metrics": post.get("public_metrics", {}),
                "retrieval_method": "x_official_mcp_search_posts_all",
                "evidence_level": "authenticated_platform_data",
            },
        })
    return {"items": items}
