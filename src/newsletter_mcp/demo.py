"""Explicitly synthetic offline fixtures. Never used as a live-provider fallback."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from newsletter_mcp.models import Draft, FocusPlan, Item, Source, Tutorial


def records(channel: str, limit: int = 6) -> dict:
    now = datetime.now(timezone.utc)
    titles = {
        "web": ["DEMO: A fictional open-model release", "DEMO: A fictional inference benchmark",
                "DEMO: A fictional developer toolkit", "DEMO: A fictional evaluation update",
                "DEMO: A fictional deployment announcement"],
        "arxiv": ["DEMO: Retrieval Evaluation Study", "DEMO: Efficient Adaptation Study"],
        "github": ["demo/evaluation-kit", "demo/local-inference-lab"],
        "producthunt": ["DEMO: Research Notebook", "DEMO: Model Comparison Workspace"],
        "twitter": ["DEMO: Researcher's evaluation checklist", "DEMO: Engineer's reproducibility note"],
    }
    items = []
    for i, title in enumerate(titles[channel][:limit]):
        items.append({"title": title, "url": f"https://example.com/demo/{channel}/{i + 1}",
                      "summary": "Synthetic fixture for testing this newsletter pipeline. This is not a real announcement, paper, repository, product or post.",
                      "published_at": (now - timedelta(days=1, hours=i)).isoformat(),
                      "metadata": {"demo": True, **({"stars": 120 + 30 * i, "date_kind": "last_push"}
                                                    if channel == "github" else {})}})
    return {"items": items, "demo": True}


def context(kind: str) -> dict:
    if kind == "google_drive":
        return {"files": [
            {"id": "demo-performance", "name": "AI Newsletter Performance Data", "mimeType": "text/csv"},
            {"id": "demo-ideas", "name": "AI Newsletter Content Ideas", "mimeType": "text/plain"},
            {"id": "demo-template", "name": "Sample Newsletter Template", "mimeType": "text/plain"},
        ], "demo": True}
    if kind == "gmail":
        return {"messages": [{"id": "demo-feedback", "subject": "DEMO newsletter feedback",
                              "text": "Synthetic reader request: explain reproducible evaluation."}], "demo": True}
    return {"events": [{"id": "demo-event", "summary": "AI Newsletter - DEMO",
                        "start": {"dateTime": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}}], "demo": True}


def focus() -> FocusPlan:
    return FocusPlan(themes=["Reproducible AI evaluation", "Local-first developer tools", "Research workflows"],
                     editorial_notes="Offline fixture plan. All research and metrics in this edition are synthetic.")


def draft(sources: list[Source]) -> Draft:
    grouped = {ch: [s for s in sources if s.channel == ch] for ch in ("web", "arxiv", "github", "producthunt", "twitter")}
    def item(source: Source) -> Item:
        return Item(title=source.title, body="This synthetic example demonstrates a sourced newsletter item. It is included only to verify the pipeline and must not be published as real news.", source_ids=[source.id])
    return Draft(title="DEMO EDITION - The AI Dispatch",
                 preheader="Synthetic offline fixtures. Not a live news edition.",
                 introduction="This is a demonstration of the nine-section newsletter workflow. Every research item and metric is synthetic; no external accounts or model APIs were contacted.",
                 big_story=item(grouped["web"][0]) if grouped["web"] else None,
                 quick_updates=[item(s) for s in grouped["web"][1:5]],
                 research=[item(s) for s in grouped["arxiv"][:3]],
                 repositories=[item(s) for s in grouped["github"][:3]],
                 tutorial=Tutorial(title="Check a source record before drafting", lesson_type="mini_tutorial",
                     objective="Learn how to confirm that a newsletter claim has a usable source record.",
                     explanation="A reliable newsletter item begins with a source record that separates publication evidence from retrieval metadata. Checking those fields before drafting prevents an activity timestamp or repository update from being presented as a new public announcement.", steps=[
                     "Confirm that each record has a title, an original link and a retrieval timestamp.",
                     "Distinguish a publication date from a repository's last-push date.",
                     "Stop publication when a claim is not supported by its cited source."],
                     code='required = {"title", "url", "retrieved_at"}\nassert required.issubset(source_record)',
                     takeaway="Treat source metadata as evidence with defined limits, not as proof of every claim.", source_ids=[]),
                 products=[item(s) for s in grouped["producthunt"][:3]],
                 tweets=[item(s) for s in grouped["twitter"][:3]],
                 closing="End of the synthetic demonstration. Configure live MCP credentials and choose Ollama or Groq to research a real edition.")
