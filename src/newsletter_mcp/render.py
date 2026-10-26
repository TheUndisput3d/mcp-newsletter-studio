from __future__ import annotations

import re
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from newsletter_mcp.models import Draft, Source, safe_url

SECTION_NAMES = ["Introduction", "Big Story of the Week", "Quick Updates", "Top Research Papers",
                 "Top GitHub Repositories", "Learning Corner of the Week", "Top AI Products",
                 "Top Tweets of the Week", "Closing Notes"]


def display_source(source: Source) -> Source:
    """Keep one trusted href while preventing URL fragments in untrusted source titles."""
    title = re.sub(r"(?:https?://|www\.)[^\s<>\[\]()\"']+", "linked resource", source.title, flags=re.I)
    return source.model_copy(update={"title": title})


def section_data(draft: Draft, sources: list[Source], channel_status: dict) -> list[dict]:
    lookup = {s.id: s for s in sources}
    def item_data(item):
        data = item.model_dump()
        data["sources"] = [display_source(lookup[i]) for i in item.source_ids]
        return data
    def group(title, items, channel):
        return {"name": title, "items": [item_data(i) for i in items],
                "notice": "" if items else channel_status.get(channel, {}).get("reason", "No qualifying sources were available for this edition.")}
    sections = [
        {"name": SECTION_NAMES[0], "text": draft.introduction},
        group(SECTION_NAMES[1], [draft.big_story] if draft.big_story else [], "web"),
        group(SECTION_NAMES[2], draft.quick_updates, "web"),
        group(SECTION_NAMES[3], draft.research, "arxiv"),
        group(SECTION_NAMES[4], draft.repositories, "github"),
        {"name": SECTION_NAMES[5], "tutorial": draft.tutorial.model_dump(),
         "sources": [display_source(lookup[i]) for i in draft.tutorial.source_ids]},
        group(SECTION_NAMES[6], draft.products, "producthunt"),
        group("AI Community Highlights" if channel_status.get("twitter", {}).get("fallback") else SECTION_NAMES[7], draft.tweets, "twitter"),
        {"name": SECTION_NAMES[8], "text": draft.closing},
    ]
    return sections


def source_details(source: Source) -> str:
    details = []
    metadata = source.metadata
    if source.channel == "github":
        if metadata.get("stars") is not None:
            details.append(f"{metadata['stars']:,} total stars at retrieval")
        delta = metadata.get("star_change_since_previous_observation")
        if delta is not None:
            details.append(f"{delta:+,} since observation {metadata['previous_observed_at'][:10]}")
        details.append("Selected from recently pushed repositories, not a verified trending ranking")
    if source.channel == "arxiv":
        details.append("Based on the abstract; peer-review status not established")
    if metadata.get("retrieval_method") == "tavily_fallback":
        details.append("Discovered through Tavily public web search; not authenticated platform data")
        if source.channel == "twitter":
            details.append("Community highlight, not a verified top-tweet ranking")
    if source.published_at:
        label = "Last push" if metadata.get("date_kind") == "last_push" else "Source date"
        details.append(f"{label}: {source.published_at[:10]}")
    else:
        details.append("Publication date unavailable")
    return ". ".join(details)


def md_escape(text: str) -> str:
    # Markdown is the phase-3 source of truth; prevent source titles from becoming markup.
    return re.sub(r"([\[\]<>])", r"\\\1", text)


def render_issue(draft: Draft, sources: list[Source], channel_status: dict, *, name: str,
                 author: str, issue_date: str, demo: bool, include_html: bool = True) -> dict[str, str]:
    sections = section_data(draft, sources, channel_status)
    html = ""
    if include_html:
        environment = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                                  autoescape=select_autoescape(["html"]))
        environment.globals.update(source_details=source_details, safe_url=safe_url)
        html = environment.get_template("email.html").render(
            draft=draft, sections=sections, name=name, author=author, issue_date=issue_date, demo=demo)
    markdown = [f"# {md_escape(draft.title)}", "", f"Edition: {issue_date}", ""]
    text = [draft.title.upper(), f"Edition: {issue_date}", ""]
    if demo:
        markdown += ["> DEMO: All research and metrics are synthetic. Do not publish as news.", ""]
        text += ["DEMO: All research and metrics are synthetic. Do not publish as news.", ""]
    for section in sections:
        markdown += ["## " + section["name"], ""]
        text += ["---", section["name"].upper(), ""]
        if section.get("text"):
            markdown += [md_escape(section["text"]), ""]
            text += [section["text"], ""]
        if section.get("notice"):
            markdown += ["_" + section["notice"] + "_", ""]
            text += [section["notice"], ""]
        for item in section.get("items", []):
            markdown += ["### " + md_escape(item["title"]), "", md_escape(item["body"]), ""]
            text += [item["title"], item["body"], ""]
            for source in item["sources"]:
                markdown += [f"Source: [{md_escape(source.title)}](<{source.url}>)"]
                text += [f"Source: {source.title} ({source.url})"]
                details = source_details(source)
                if details:
                    markdown += [details]
                    text += [details]
                pdf = source.metadata.get("pdf_url")
                if pdf:
                    try:
                        safe_url(pdf)
                        markdown += [f"[Paper PDF](<{pdf}>)"]
                        text += [f"Paper PDF ({pdf})"]
                    except ValueError:
                        pass
            markdown.append("")
            text.append("")
        tutorial = section.get("tutorial")
        if tutorial:
            markdown += ["### " + md_escape(tutorial["title"]), ""]
            text += [tutorial["title"], ""]
            markdown += ["**What you'll learn:** " + md_escape(tutorial["objective"]), "",
                         md_escape(tutorial["explanation"]), ""]
            text += ["What you'll learn: " + tutorial["objective"], "", tutorial["explanation"], ""]
            if tutorial["steps"]:
                markdown += ["**Try it:**", ""]
                text += ["Try it:", ""]
            for number, step in enumerate(tutorial["steps"], 1):
                markdown += [f"{number}. {md_escape(step)}"]
                text += [f"{number}. {step}"]
            if tutorial.get("code"):
                markdown += ["", "~~~~", tutorial["code"], "~~~~", "", "Review code before running it."]
                text += ["", tutorial["code"], "", "Review code before running it."]
            markdown += ["", "**Takeaway:** " + md_escape(tutorial["takeaway"])]
            text += ["", "Takeaway: " + tutorial["takeaway"]]
            for source in section["sources"]:
                markdown += [f"Source: [{md_escape(source.title)}](<{source.url}>)"]
                text += [f"Source: {source.title} ({source.url})"]
            markdown.append("")
            text.append("")
    markdown += ["---", author, ""]
    text += ["---", author, ""]
    plain = "\n".join(text)
    if not include_html:
        return {"final_draft_newsletter.md": "\n".join(markdown)}
    message = EmailMessage(policy=SMTP)
    message["Subject"] = draft.title
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    return {"final_draft_newsletter.md": "\n".join(markdown), "newsletter.html": html,
            "newsletter.txt": plain, "newsletter.eml": message.as_string()}
