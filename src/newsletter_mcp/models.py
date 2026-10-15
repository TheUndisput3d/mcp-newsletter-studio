from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Channel = Literal["web", "arxiv", "github", "producthunt", "twitter"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_url(url: str) -> str:
    value = urlsplit(url)
    if value.scheme not in {"http", "https"} or not value.hostname or value.username or value.password:
        raise ValueError("Only absolute HTTP(S) source URLs without embedded credentials are allowed")
    if any(ord(c) < 32 for c in url) or any(c in url for c in '<>"'):
        raise ValueError("Unsafe characters in source URL")
    return url


def canonical_url(url: str) -> str:
    value = urlsplit(safe_url(url))
    query = [(k, v) for k, v in parse_qsl(value.query) if not k.lower().startswith("utm_")]
    return urlunsplit((value.scheme.lower(), value.netloc.lower(), value.path.rstrip("/"), urlencode(query), ""))


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    channel: Channel
    title: str = Field(max_length=1000)
    url: str
    summary: str = Field(default="", max_length=12000)
    published_at: str | None = None
    retrieved_at: str = Field(default_factory=utc_now)
    metadata: dict = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return safe_url(value)

    @classmethod
    def from_record(cls, channel: Channel, record: dict) -> "Source":
        url = safe_url(str(record["url"]))
        identifier = channel + "_" + hashlib.sha256(canonical_url(url).encode()).hexdigest()[:12]
        return cls(id=identifier, channel=channel, title=str(record.get("title", "Untitled"))[:1000],
                   url=url, summary=str(record.get("summary", ""))[:12000],
                   published_at=record.get("published_at"), retrieved_at=record.get("retrieved_at", utc_now()),
                   metadata=record.get("metadata", {}))


class PlainTextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def no_urls_or_html(cls, value, info):
        if info.field_name in {"title", "preheader"} and isinstance(value, str) and any(c in value for c in "\r\n"):
            raise ValueError("Titles and preheaders must be a single line")
        texts = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        for text in texts:
            if isinstance(text, str) and (re.search(r"https?://|www\.", text, re.I)
                                         or re.search(r"<\s*/?[a-zA-Z][^>]*>", text)):
                raise ValueError("Write plain text; reference links with source_ids, not URLs or HTML")
        return value


class Item(PlainTextModel):
    title: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=1, max_length=2400)
    source_ids: list[str] = Field(min_length=1, max_length=5)


class Tutorial(PlainTextModel):
    title: str = Field(min_length=1, max_length=180)
    lesson_type: Literal["concept", "mini_tutorial"]
    objective: str = Field(min_length=1, max_length=240)
    explanation: str = Field(min_length=80, max_length=1600)
    steps: list[str] = Field(default_factory=list, max_length=4)
    code: str = Field(default="", max_length=1500)
    takeaway: str = Field(min_length=1, max_length=400)
    source_ids: list[str] = Field(default_factory=list, max_length=5)


class Draft(PlainTextModel):
    title: str = Field(min_length=1, max_length=180)
    preheader: str = Field(min_length=1, max_length=180)
    introduction: str = Field(min_length=1, max_length=1400)
    big_story: Item | None
    quick_updates: list[Item] = Field(max_length=5)
    research: list[Item] = Field(max_length=5)
    repositories: list[Item] = Field(max_length=5)
    tutorial: Tutorial
    products: list[Item] = Field(max_length=5)
    tweets: list[Item] = Field(max_length=5)
    closing: str = Field(min_length=1, max_length=1000)


class EditionCopy(PlainTextModel):
    """Small, flat editorial copy generated separately from structured sections."""
    title: str = Field(min_length=1, max_length=180)
    preheader: str = Field(min_length=1, max_length=180)
    introduction: str = Field(min_length=1, max_length=1400)
    closing: str = Field(min_length=1, max_length=1000)


class ItemCollection(PlainTextModel):
    """A bounded group of items for one research channel."""
    items: list[Item] = Field(max_length=5)


class EditorialItemCopy(PlainTextModel):
    """Model-written prose; trusted source IDs are attached by the pipeline."""
    title: str = Field(min_length=1, max_length=180)
    body: str = Field(min_length=1, max_length=2400)


class LearningTopicSelection(PlainTextModel):
    """A bounded public-research choice; array indexes are validated in code."""
    primary_index: int = Field(ge=1, le=12)
    lesson_type: Literal["concept", "mini_tutorial"]
    lesson_angle: str = Field(min_length=1, max_length=320)


class TutorialCopy(PlainTextModel):
    """One-minute lesson copy; trusted source IDs are attached by the pipeline."""
    title: str = Field(min_length=1, max_length=180)
    lesson_type: Literal["concept", "mini_tutorial"]
    objective: str = Field(min_length=1, max_length=240)
    explanation: str = Field(min_length=80, max_length=1600)
    steps: list[str] = Field(default_factory=list, max_length=4)
    code: str = Field(default="", max_length=1500)
    takeaway: str = Field(min_length=1, max_length=400)


class FocusPlan(PlainTextModel):
    themes: list[str] = Field(min_length=1, max_length=4)
    editorial_notes: str = Field(max_length=1600)


def validate_references(draft: Draft, sources: list[Source]) -> None:
    lookup = {s.id: s for s in sources}
    groups = [([draft.big_story] if draft.big_story else [], "web"),
              (draft.quick_updates, "web"), (draft.research, "arxiv"),
              (draft.repositories, "github"), (draft.products, "producthunt"),
              (draft.tweets, "twitter")]
    seen: set[str] = set()
    for items, expected in groups:
        for item in items:
            for source_id in item.source_ids:
                if source_id not in lookup:
                    raise ValueError(f"Unknown source id: {source_id}")
                if lookup[source_id].channel != expected:
                    raise ValueError(f"Source {source_id} does not belong in {expected}")
            primary = item.source_ids[0]
            if primary in seen:
                raise ValueError("The same primary source was reused in multiple editorial items")
            seen.add(primary)
    if any(s.channel == "web" for s in sources) and draft.big_story is None:
        raise ValueError("A sourced big story is required when web research is available")
    web_count = sum(s.channel == "web" for s in sources)
    if len(draft.quick_updates) < min(3, max(0, web_count - 1)):
        raise ValueError("Provide three quick updates when enough distinct web sources are available")
    for items, expected in groups[2:]:
        if any(s.channel == expected for s in sources) and not items:
            raise ValueError(f"Available {expected} sources require at least one editorial item")
    for source_id in draft.tutorial.source_ids:
        if source_id not in lookup:
            raise ValueError(f"Unknown tutorial source: {source_id}")
