"""Small, source-bounded editorial calls assembled into the public Draft contract."""
from __future__ import annotations

import re

from newsletter_mcp.llm import LLMClient
from newsletter_mcp.models import (
    Draft,
    EditionCopy,
    EditorialItemCopy,
    Item,
    LearningTopicSelection,
    Source,
    Tutorial,
    TutorialCopy,
    validate_references,
)


def compact_sources(sources: list[Source]) -> list[dict]:
    """Only pass evidence a stage can cite; URLs remain renderer-owned data."""
    return [{"id": source.id, "channel": source.channel, "title": source.title,
             "summary": source.summary[:700], "published_at": source.published_at}
            for source in sources]


async def _item_group(llm: LLMClient, *, label: str, sources: list[Source], brief: dict,
                      private_context: dict) -> list[Item]:
    if not sources:
        return []
    items = []
    for source in sources[:5]:
        task = (f"Write one concise {label} item based only on the supplied source. "
                "Return only a title and body. Do not write URLs, HTML, source IDs, unsupported metrics, dates, or quotes.")
        copy = await llm.generate(EditorialItemCopy, task,
                                  brief | {"source": compact_sources([source])[0]},
                                  private_context, task_kind="draft")
        # Source IDs are trusted renderer-owned data, never model-generated text.
        items.append(Item(title=copy.title, body=copy.body, source_ids=[source.id]))
    return items


def learning_candidates(sources: list[Source]) -> list[Source]:
    """Build a small, channel-balanced portfolio from public research only."""
    limits = {"web": 3, "arxiv": 3, "github": 3, "producthunt": 1}
    candidates = [source for channel, limit in limits.items()
                  for source in [item for item in sources if item.channel == channel][:limit]]
    return candidates[:10] or sources[:10]


async def _tutorial(llm: LLMClient, *, sources: list[Source], brief: dict) -> Tutorial:
    """Select and write one source-grounded Learning of the Week using public research only."""
    if not sources:
        return Tutorial(title="Learning corner unavailable", lesson_type="concept",
                        objective="Understand why this edition has no source-grounded lesson.",
                        explanation="No qualifying public research source was available for a reliable one-minute lesson. Review the research inputs before adding educational content to this edition.",
                        steps=[], code="", takeaway="Publish a lesson only when current public evidence supports it.",
                        source_ids=[])

    candidates = learning_candidates(sources)
    selection_task = (
        "Choose the strongest topic for a one-minute Learning of the Week using only these current public "
        "research candidates. Prefer a reusable AI concept, mechanism, engineering technique, or practical "
        "skill with enough technical substance to teach. Avoid event recaps, generic news summaries, product "
        "promotion, and generic source-checking advice. Choose concept when the evidence supports an explanation; "
        "choose mini_tutorial only when it supports actionable steps. Use one 1-based candidate index."
    )

    def validate_selection(value: LearningTopicSelection) -> None:
        if value.primary_index < 1 or value.primary_index > len(candidates):
            raise ValueError("Learning topic selection contains an out-of-range candidate index")

    selection = await llm.generate(
        LearningTopicSelection, selection_task,
        {"audience": brief.get("audience"), "issue_date": brief.get("issue_date"),
         "candidates": [{"index": index, **{key: value for key, value in compact_sources([source])[0].items()
                                             if key != "id"}}
                        for index, source in enumerate(candidates, 1)]},
        None, validator=validate_selection, task_kind="small")
    selected = [candidates[selection.primary_index - 1]]
    task = (
        "Write a one-minute Learning of the Week from only the supplied public evidence and selected lesson "
        "angle. This is a useful lesson, not an assignment, quiz, news recap, or product advertisement. Return "
        "a concrete title, one-sentence objective, clear explanation, and a specific takeaway. For concept mode, "
        "write a coherent conceptual explanation with no steps or code. For mini_tutorial mode, provide two to "
        "four short actionable steps and optional code only when the evidence directly supports the syntax. Keep "
        "the prose between 55 and 240 words. Use no outside facts, URLs, HTML, source IDs, unsupported metrics, "
        "dates, or quotes. Never claim generated code was tested or executed."
    )
    source_text = " ".join(part for source in selected for part in (source.title, source.summary))
    source_lower = source_text.lower()
    source_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", source_text))
    generic_terms = {"ai"}

    def validate_grounding(copy: TutorialCopy) -> None:
        prose = " ".join((copy.title, copy.objective, copy.explanation, *copy.steps, copy.takeaway))
        word_count = len(re.findall(r"\b\w+[\w'-]*\b", " ".join(
            (copy.objective, copy.explanation, *copy.steps, copy.takeaway))))
        if not 55 <= word_count <= 240:
            raise ValueError(f"Learning Corner prose must contain 55-240 words; received {word_count}")
        if copy.lesson_type != selection.lesson_type:
            raise ValueError("Learning Corner type must match the selected lesson type")
        if copy.lesson_type == "concept" and (copy.steps or copy.code.strip()):
            raise ValueError("Concept lessons must use explanation only, without steps or code")
        if copy.lesson_type == "mini_tutorial" and not 2 <= len(copy.steps) <= 4:
            raise ValueError("Mini tutorials require two to four actionable steps")
        unsupported_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", prose)) - source_numbers
        unsupported_names: set[str] = set()
        for passage in (copy.title, copy.objective, copy.explanation, *copy.steps, copy.takeaway):
            for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9.-]*\b", passage):
                token = match.group(0)
                before = passage[:match.start()].rstrip()
                looks_named = ((token.isupper() and len(token) > 1)
                               or (any(char.isupper() for char in token[1:])
                                   and any(char.islower() for char in token)))
                if looks_named and token.lower() not in source_lower and token.lower() not in generic_terms:
                    unsupported_names.add(token)
        if unsupported_numbers:
            raise ValueError("Unsupported numbers: " + ", ".join(sorted(unsupported_numbers)))
        if unsupported_names:
            raise ValueError("Unsupported named terms: " + ", ".join(sorted(unsupported_names)))

    copy = await llm.generate(
        TutorialCopy, task,
        {"audience": brief.get("audience"), "issue_date": brief.get("issue_date"),
         "lesson_type": selection.lesson_type, "lesson_angle": selection.lesson_angle,
         "evidence": compact_sources(selected)},
        None, validator=validate_grounding, task_kind="small")
    return Tutorial(**copy.model_dump(), source_ids=[source.id for source in selected])


async def build_staged_draft(llm: LLMClient, *, brief: dict, sources: list[Source],
                             focus_plan: dict, private_context: dict) -> Draft:
    """Generate compact, independently validated parts then preserve the old Draft schema."""
    local_private = {"focus_plan": focus_plan}
    copy_task = ("Write only the edition title, preheader, introduction, and closing. "
                 "Keep the introduction high level: do not introduce facts, metrics, dates, quotes, URLs, or HTML. "
                 "Use the editorial brief and focus plan as style guidance.")
    copy = await llm.generate(EditionCopy, copy_task, brief, local_private, task_kind="draft")
    by_channel = {channel: [source for source in sources if source.channel == channel]
                  for channel in ("web", "arxiv", "github", "producthunt", "twitter")}
    web = by_channel["web"]
    big_story = None
    if web:
        result = await _item_group(llm, label="lead story", sources=web[:1], brief=brief,
                                   private_context=local_private)
        big_story = result[0]
    # Preserve the original editorial scope (one lead, up to three quick updates,
    # and up to three items per non-web channel) within the 30-call safety cap.
    quick_updates = await _item_group(llm, label="quick updates", sources=web[1:4], brief=brief,
                                      private_context=local_private)
    research = await _item_group(llm, label="research highlights", sources=by_channel["arxiv"][:3], brief=brief,
                                 private_context=local_private)
    repositories = await _item_group(llm, label="repository highlights", sources=by_channel["github"][:3], brief=brief,
                                    private_context=local_private)
    products = await _item_group(llm, label="product highlights", sources=by_channel["producthunt"][:3], brief=brief,
                                 private_context=local_private)
    tweets = await _item_group(llm, label="community highlights", sources=by_channel["twitter"][:3], brief=brief,
                               private_context=local_private)
    tutorial = await _tutorial(llm, sources=sources, brief=brief)
    draft = Draft(title=copy.title, preheader=copy.preheader, introduction=copy.introduction,
                  big_story=big_story, quick_updates=quick_updates, research=research,
                  repositories=repositories, tutorial=tutorial, products=products,
                  tweets=tweets, closing=copy.closing)
    validate_references(draft, sources)
    return draft
