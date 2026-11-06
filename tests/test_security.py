from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from newsletter_mcp.demo import draft, records
from newsletter_mcp.models import Source, safe_url, validate_references
from newsletter_mcp.render import display_source
from newsletter_mcp.pipeline import normalize_sources
from newsletter_mcp.registry import external_environment


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "https://user:secret@example.com", "https://example.com/\nheader", 'https://example.com/\"x'])
def test_unsafe_source_urls(url):
    with pytest.raises(ValueError):
        safe_url(url)


def test_source_date_filter_and_deduplication():
    now = datetime.now(timezone.utc)
    source = {"url": "https://example.com/a?utm_source=test", "title": "Dated", "published_at": (now - timedelta(days=1)).isoformat()}
    inputs = [source, source | {"url": "https://example.com/a"},
              source | {"url": "https://example.com/old", "published_at": (now - timedelta(days=40)).isoformat()},
              source | {"url": "https://example.com/undated", "published_at": None}]
    results, excluded = normalize_sources("web", inputs, now - timedelta(days=7), now, False, set(), 10)
    assert len(results) == 1 and len(excluded) == 2


def test_unknown_citation_and_wrong_channel_are_rejected():
    sources = [Source.from_record("web", record) for record in records("web")["items"]]
    issue = draft(sources)
    validate_references(issue, sources)
    issue.big_story.source_ids = ["invented"]
    with pytest.raises(ValueError, match="Unknown"):
        validate_references(issue, sources)


def test_model_prose_cannot_invent_urls():
    from newsletter_mcp.models import Item
    with pytest.raises(ValidationError):
        Item(title="Fake link", body="See https://invented.invalid", source_ids=["web_one"])


def test_source_display_title_strips_url_fragments_without_changing_href():
    source = Source(id="web_title", channel="web", title="Read https://t.co/abc now", url="https://example.com/original")
    displayed = display_source(source)
    assert "https://" not in displayed.title
    assert displayed.url == source.url


def test_external_server_environment_is_allowlisted(settings, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-tavily-secret")
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-secret")
    monkeypatch.setenv("X_BEARER_TOKEN", "fake-x-secret")
    file_env = external_environment(settings, {})
    assert "TAVILY_API_KEY" not in file_env and "GROQ_API_KEY" not in file_env
    tavily_env = external_environment(settings, {"TAVILY_API_KEY": "${TAVILY_API_KEY}"})
    assert tavily_env["TAVILY_API_KEY"] == "fake-tavily-secret"
    assert "GROQ_API_KEY" not in tavily_env and "X_BEARER_TOKEN" not in tavily_env
