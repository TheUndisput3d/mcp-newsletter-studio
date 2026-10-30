from newsletter_mcp.registry import (
    MCPHub,
    normalize_arxiv_result,
    normalize_github_result,
    normalize_producthunt_result,
    normalize_web_result,
    normalize_x_result,
    validate_official_x_contract,
)
from newsletter_mcp.config import MCPConfig
from newsletter_mcp.protocol import MCPError
import pytest


def test_tavily_mcp_result_normalization():
    result = normalize_web_result({"results": [{"title": "Release", "url": "https://example.com/release",
        "content": "Evidence", "published_date": "Wed, 04 Nov 2026 08:06:53 GMT"}]})
    assert result["items"][0]["published_at"].startswith("2026-11-04T")


def test_arxiv_mcp_result_normalization():
    result = normalize_arxiv_result({"papers": [{"id": "1", "title": "Paper", "abstract": "Abstract",
        "abstract_url": "https://arxiv.org/abs/1", "pdf_url": "https://arxiv.org/pdf/1",
        "published": "2026-11-04T00:00:00Z", "authors": ["A"]}]})
    assert result["items"][0]["metadata"]["evidence_level"] == "abstract"


def test_github_mcp_result_normalization():
    result = normalize_github_result({"items": [{"full_name": "o/r", "html_url": "https://github.com/o/r",
        "description": "Repo", "pushed_at": "2026-11-04T00:00:00Z", "stargazers_count": 7}]})
    assert result["items"][0]["metadata"]["stars"] == 7


async def test_github_search_uses_star_ranking_and_activity_window(settings):
    captured = {}

    class Client:
        async def call(self, name, arguments):
            captured.update(name=name, arguments=arguments)
            return {"items": []}

    hub = MCPHub(settings)
    hub.status["github"] = {"state": "ready"}
    hub.clients["github"] = Client()
    await hub.call("github", "search_repositories", {
        "query": "topic:artificial-intelligence archived:false fork:false stars:>100",
        "start_time": "2026-11-03T00:00:00+00:00",
        "end_time": "2026-11-10T00:00:00+00:00",
        "max_results": 6,
    })
    assert captured["name"] == "search_repositories"
    assert captured["arguments"]["query"] == (
        "topic:artificial-intelligence archived:false fork:false stars:>100 "
        "pushed:2026-11-03..2026-11-10"
    )
    assert captured["arguments"]["perPage"] == 6
    assert captured["arguments"]["sort"] == "stars"
    assert captured["arguments"]["order"] == "desc"


def test_producthunt_mcp_result_normalization():
    result = normalize_producthunt_result({"status": "success", "data": {"products": [{
        "name": "AI Launch", "url": "https://www.producthunt.com/products/ai-launch?ref=mcp",
        "description": "A new AI product", "tagline": "Ship with AI",
        "launched_at": "2026-11-09T00:01:00-08:00", "votes_count": 42,
        "categories": [{"name": "Artificial Intelligence", "slug": "artificial-intelligence"}],
    }]}})
    item = result["items"][0]
    assert item["title"] == "AI Launch"
    assert item["published_at"] == "2026-11-09T00:01:00-08:00"
    assert item["metadata"]["retrieval_method"] == "producthunt_official_mcp"
    assert item["metadata"]["votes"] == 42


def test_official_x_mcp_result_normalization():
    result = normalize_x_result({
        "data": [{
            "id": "1234567890123456789",
            "text": "A useful AI systems release",
            "author_id": "42",
            "created_at": "2026-11-08T12:00:00.000Z",
            "lang": "en",
            "public_metrics": {"like_count": 17, "repost_count": 3},
        }],
        "includes": {"users": [{"id": "42", "username": "ai_builder", "name": "AI Builder", "verified": True}]},
    })
    item = result["items"][0]
    assert item["url"] == "https://x.com/ai_builder/status/1234567890123456789"
    assert item["published_at"] == "2026-11-08T12:00:00.000Z"
    assert item["metadata"]["retrieval_method"] == "x_official_mcp_search_posts_all"
    assert item["metadata"]["evidence_level"] == "authenticated_platform_data"


def test_official_x_mcp_contract_is_strict():
    properties = {
        "query": {"type": "string"}, "start_time": {"type": "string"},
        "end_time": {"type": "string"}, "max_results": {"type": "integer"},
        "post.fields": {"type": "string"}, "expansions": {"type": "string"},
        "user.fields": {"type": "string"},
    }
    tool = {"name": "search_posts_all", "inputSchema": {
        "type": "object", "properties": properties, "required": ["query"]}}
    validate_official_x_contract(
        "https://api.x.com/mcp", {"search_posts": "search_posts_all"}, {"name": "xmcp"},
        {"search_posts_all": tool})
    with pytest.raises(MCPError, match="official hosted"):
        validate_official_x_contract(
            "https://example.com/mcp", {"search_posts": "search_posts_all"}, {"name": "xmcp"},
            {"search_posts_all": tool})
    with pytest.raises(MCPError, match="search_posts_all"):
        validate_official_x_contract(
            "https://api.x.com/mcp", {"search_posts": "search_posts"}, {"name": "xmcp"},
            {"search_posts_all": tool})


async def test_x_search_uses_the_official_full_archive_arguments(settings):
    captured = {}

    class Client:
        async def call(self, name, arguments):
            captured.update(name=name, arguments=arguments)
            return {"data": [{
                "id": "1", "text": "AI release", "created_at": "2026-11-08T00:00:00Z",
            }]}

    settings.mcps["twitter"] = MCPConfig(
        enabled=True, transport="streamable_http", authentication="bearer",
        url="https://api.x.com/mcp", token_env="X_BEARER_TOKEN",
        operations={"search_posts": "search_posts_all"},
    )
    hub = MCPHub(settings)
    hub.status["twitter"] = {"state": "ready"}
    hub.clients["twitter"] = Client()
    result = await hub.call("twitter", "search_posts", {
        "query": '(AI OR "machine learning") lang:en -is:retweet',
        "start_time": "2026-11-02T00:00:00+00:00",
        "end_time": "2026-11-09T00:00:00+00:00",
        "max_results": 6,
    })
    assert captured["name"] == "search_posts_all"
    assert captured["arguments"]["max_results"] == 10
    assert captured["arguments"]["sort_order"] == "recency"
    assert "created_at" in captured["arguments"]["post.fields"]
    assert "author_id" not in captured["arguments"]["post.fields"]
    assert captured["arguments"]["expansions"] == "author_id"
    assert captured["arguments"]["end_time"] == "2026-11-08T23:59:45Z"
    assert result["items"][0]["url"] == "https://x.com/i/web/status/1"
