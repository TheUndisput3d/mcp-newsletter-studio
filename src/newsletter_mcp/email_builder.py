"""Phase 3: saved Markdown -> LLM HTML/TXT, with explicit opt-in Jinja recovery only."""
from __future__ import annotations

import hashlib
import html as html_module
import re
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from bs4 import BeautifulSoup, Comment
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup
from pydantic import BaseModel, ConfigDict, Field
import tinycss2

from newsletter_mcp.config import Settings
from newsletter_mcp.llm import HTML_RULES, LLMClient, LLMError
from newsletter_mcp.models import safe_url
from newsletter_mcp.render import SECTION_NAMES


class EmailPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str = Field(min_length=300, max_length=700000)
    text: str = Field(min_length=50, max_length=150000)


class EmailFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str = Field(min_length=20, max_length=100000)


ALLOWED_TAGS = {"html", "head", "meta", "title", "body", "table", "tbody", "thead", "tfoot", "tr", "td", "th",
                "div", "span", "p", "a", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "code",
                "blockquote", "strong", "b", "em", "i", "u", "br", "hr"}
ALLOWED_ATTRIBUTES = {"lang", "charset", "name", "content", "style", "class", "id", "role", "width", "height",
                      "cellpadding", "cellspacing", "border", "align", "valign", "colspan", "rowspan", "href",
                      "target", "rel", "title", "dir", "start", "type", "aria-label", "aria-hidden"}


def normalize(text: str) -> str:
    return " ".join(html_module.unescape(text).split()).casefold()


def markdown_soup(markdown: str) -> BeautifulSoup:
    # Raw HTML is never trusted, even when a user edits Markdown.
    return BeautifulSoup(MarkdownIt("commonmark", {"html": False}).render(markdown), "html.parser")


def validate_markdown(markdown: str) -> BeautifulSoup:
    if len(markdown) > 150000:
        raise ValueError("Markdown exceeds the 150,000-character limit")
    soup = markdown_soup(markdown)
    headings = [n.get_text(" ", strip=True) for n in soup.find_all("h2")]
    expected = SECTION_NAMES.copy()
    if len(headings) > 7 and headings[7] == "AI Community Highlights":
        expected[7] = "AI Community Highlights"
    if headings != expected:
        raise ValueError("Markdown must retain the nine original section headings, in order")
    if soup.find("img"):
        raise ValueError("Remote or embedded images are not allowed in newsletter Markdown")
    for link in soup.find_all("a"):
        safe_url(link.get("href", ""))
    return soup


def check_css(style: str) -> list[str]:
    errors = []
    declarations = tinycss2.parse_declaration_list(style, skip_comments=True, skip_whitespace=True)
    def unsafe(nodes):
        for node in nodes:
            if node.type in {"url", "error", "at-rule"}:
                return True
            if node.type == "function":
                if node.lower_name in {"url", "expression"} or unsafe(node.arguments):
                    return True
            if hasattr(node, "content") and isinstance(node.content, list) and unsafe(node.content):
                return True
        return False
    for declaration in declarations:
        if declaration.type != "declaration":
            errors.append("Malformed or non-inline CSS")
            continue
        value = tinycss2.serialize(declaration.value).strip().lower()
        if declaration.lower_name in {"behavior", "-moz-binding"} or unsafe(declaration.value):
            errors.append("Unsafe CSS")
        if (declaration.lower_name == "display" and value == "none") or (declaration.lower_name == "visibility" and value == "hidden") or (declaration.lower_name == "opacity" and value in {"0", "0.0"}):
            errors.append("Hidden content is not allowed")
    return errors


def validate_package(package: EmailPackage, markdown: str, *, content_check: bool = True) -> dict:
    reference = validate_markdown(markdown)
    soup = BeautifulSoup(package.html, "html.parser")
    issues = []
    if not soup.html or not soup.body or "</html>" not in package.html.lower():
        issues.append("Complete html and body elements with a closing html tag are required")
    if not soup.find("table"):
        issues.append("Use a table-based email layout")
    if not re.search(r"max-width\s*:\s*600px", package.html, re.I):
        issues.append("A 600px maximum-width container is required")
    for color in ("#1e293b", "#334155", "#2563eb", "#f8fafc"):
        if color not in package.html.lower():
            issues.append("Missing design color " + color)
    if "sans-serif" not in package.html.lower():
        issues.append("Use the required sans-serif font stack")
    for node in soup.find_all(True):
        if node.name not in ALLOWED_TAGS:
            issues.append("Disallowed element: " + node.name)
        for attribute in node.attrs:
            if attribute not in ALLOWED_ATTRIBUTES:
                issues.append("Disallowed attribute: " + attribute)
        if node.name == "meta" and (node.get("http-equiv") or node.get("name", "").lower() not in {"", "viewport", "color-scheme"}):
            issues.append("Only charset, viewport and color-scheme metadata are allowed")
        issues.extend(check_css(node.get("style", "")))
    # Conditional comments can hide executable markup from ordinary DOM traversal.
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        if re.search(r"<\s*(script|iframe|object|embed|link|style|svg|form|img)|\bon\w+\s*=|javascript:", str(comment), re.I):
            issues.append("Unsafe conditional comment")
    expected_headings = [normalize(h.get_text(" ", strip=True)) for h in reference.find_all("h2")]
    headings = [normalize(h.get_text(" ", strip=True)) for h in soup.find_all("h2")]
    if headings != expected_headings:
        issues.append("HTML must have exactly nine h2 headings matching Markdown, in order")
    expected_links = {a["href"] for a in reference.find_all("a", href=True)}
    actual_links = set()
    for link in soup.find_all("a"):
        url = link.get("href", "")
        try:
            safe_url(url)
        except ValueError:
            issues.append("Unsafe or missing hyperlink")
        actual_links.add(url)
        if link.get("target") != "_blank":
            issues.append("Every source link must have target=_blank")
        rel = set(link.get("rel", []))
        if not {"noopener", "noreferrer"}.issubset(rel):
            issues.append("Every source link must use rel=noopener noreferrer")
    if actual_links != expected_links:
        issues.append("HTML source URLs differ from Markdown; preserve every URL without additions")
    plain_urls = set(re.findall(r"https?://[^\s<>\"']+", package.text))
    # Check actual parentheses below; raw token regex also includes trailing delimiters.
    for url in expected_links:
        if f"({url})" not in package.text:
            issues.append("Plain text must include a parenthesized URL: " + url[:160])
    for url in plain_urls:
        if not any(url == known or url.rstrip(").,;") == known.rstrip(").,;") for known in expected_links):
            issues.append("Plain text contains an unrecognized URL")
    if package.text.count("---") < 9:
        issues.append("Plain text requires section dividers")
    for heading in reference.find_all("h2"):
        if heading.get_text(" ", strip=True).upper() not in package.text:
            issues.append("Plain-text section headers must be uppercase")
    if content_check:
        # Links in plaintext introduce a URL between words; remove URLs before comparing prose.
        html_text = normalize((soup.body or soup).get_text(" ", strip=True))
        plain_text = package.text
        for url in sorted(expected_links, key=len, reverse=True):
            plain_text = plain_text.replace(f"({url})", "")
        plain_text = normalize(plain_text)
        for element in reference.find_all(["h1", "h3", "h4", "h5", "h6", "p", "pre", "li"]):
            content = normalize(element.get_text(" ", strip=True))
            if content and content not in html_text:
                issues.append("HTML omitted or rewrote text: " + content[:160])
            if content and content not in plain_text:
                issues.append("Plain text omitted or rewrote text: " + content[:160])
    if issues:
        raise ValueError("; ".join(dict.fromkeys(issues))[:6000])
    return {"safe_html": True, "nine_sections_present": True, "links_preserved": True,
            "content_preservation_checked": content_check, "source_url_count": len(expected_links),
            "email_client_matrix": "Not tested in Gmail, Outlook or Apple Mail"}


STYLES = {"p": "margin:0 0 12px;font-size:15px;line-height:1.7;color:#334155;",
          "h1": "margin:0 0 16px;font-size:30px;line-height:1.2;",
          "h2": "margin:0 0 16px;font-size:21px;line-height:1.3;color:#1e293b;",
          "h3": "margin:18px 0 8px;font-size:17px;line-height:1.4;color:#1e293b;",
          "a": "color:#2563eb;text-decoration:underline;overflow-wrap:anywhere;",
          "pre": "padding:14px;background-color:#e2e8f0;font-size:12px;white-space:pre-wrap;word-break:break-word;",
          "code": "font-family:Consolas,monospace;", "li": "margin:0 0 10px;line-height:1.7;",
          "ul": "padding-left:22px;", "ol": "padding-left:22px;", "blockquote": "margin:12px 0;padding:12px;border-left:3px solid #2563eb;"}


def markdown_parts(markdown: str) -> tuple[str, str, list[dict], str]:
    soup = validate_markdown(markdown)
    title = soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else "Newsletter"
    plain = markdown_soup(markdown)
    for link in plain.find_all("a", href=True):
        link.replace_with(link.get_text(" ", strip=True) + " (" + link["href"] + ")")
    for heading in plain.find_all("h2"):
        heading.string = "\n---\n" + heading.get_text(" ", strip=True).upper() + "\n"
    plain_text = plain.get_text("\n", strip=True) + "\n"
    for node in soup.find_all(True):
        if node.name in STYLES:
            node["style"] = STYLES[node.name]
        if node.name == "a":
            node["target"] = "_blank"
            node["rel"] = "noopener noreferrer"
    lead, sections = [], []
    current = None
    for child in list(soup.children):
        if getattr(child, "name", None) == "h1":
            continue
        if getattr(child, "name", None) == "h2":
            current = {"name": child.get_text(" ", strip=True), "parts": []}
            sections.append(current)
        elif current is None:
            lead.append(str(child))
        else:
            current["parts"].append(str(child))
    for section in sections:
        section["markdown_html"] = Markup("".join(section.pop("parts")))
    return title, "".join(lead), sections, plain_text


def template_package(markdown: str, *, name: str, author: str, issue_date: str, demo: bool) -> EmailPackage:
    """Called only behind the explicit feature flag. Uses the retained v1 Jinja email design."""
    title, lead, sections, text = markdown_parts(markdown)
    environment = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                              autoescape=select_autoescape(["html"]))
    html = environment.get_template("email.html").render(draft={"title": title}, name=name, author=author,
        issue_date=issue_date, demo=demo, sections=sections, lead_html=Markup(lead))
    return EmailPackage(html=html, text=text)


def demo_package(markdown: str) -> EmailPackage:
    """Synthetic model-output fixture, NOT a live fallback and NOT a Jinja invocation."""
    title, lead, sections, text = markdown_parts(markdown)
    cards = "".join('<table role="presentation" width="100%" style="width:100%;margin:12px 0;background-color:#f8fafc;"><tr><td style="padding:22px 20px;">'
                    + '<h2 style="' + STYLES["h2"] + '">' + html_module.escape(s["name"]) + '</h2>' + str(s["markdown_html"]) + '</td></tr></table>' for s in sections)
    document = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>' + html_module.escape(title) + '</title></head><body style="margin:0;padding:0;background-color:#eef2f6;font-family:Helvetica,Arial,sans-serif;color:#334155;">'
                '<table role="presentation" width="100%" style="width:100%;"><tr><td align="center" style="padding:28px 12px;">'
                '<table role="presentation" width="100%" style="width:100%;max-width:600px;background-color:#ffffff;"><tr><td style="padding:32px 28px;background-color:#1e293b;color:#ffffff;">'
                '<p style="margin:0 0 12px;font-size:12px;letter-spacing:2px;">MCP NEWSLETTER STUDIO / OFFLINE DEMO</p>'
                '<h1 style="' + STYLES["h1"] + '">' + html_module.escape(title) + '</h1></td></tr>'
                '<tr><td style="padding:18px 24px;">' + lead + cards + '</td></tr></table></td></tr></table></body></html>')
    return EmailPackage(html=document, text=text)


def validate_fragment(fragment: EmailFragment, source_html: str, heading: str | None) -> None:
    actual = BeautifulSoup(fragment.html, "html.parser")
    # Small local models sometimes wrap a valid fragment in a complete document
    # despite the prompt. Safely discard the wrapper before all strict checks.
    if actual.body:
        fragment.html = "".join(str(child) for child in actual.body.contents)
        actual = BeautifulSoup(fragment.html, "html.parser")
    expected = BeautifulSoup(source_html, "html.parser")
    if actual.find(["html", "head", "body", "script", "style", "iframe", "object", "embed", "img", "form", "svg"]):
        raise ValueError("Return an email-safe fragment, not a full document")
    headings = [normalize(node.get_text(" ", strip=True)) for node in actual.find_all("h2")]
    wanted = [] if heading is None else [normalize(heading)]
    if headings != wanted:
        raise ValueError("Fragment must preserve its h2 heading exactly")
    for node in actual.find_all(True):
        if node.name not in ALLOWED_TAGS:
            raise ValueError("Disallowed fragment element: " + node.name)
        for attribute in node.attrs:
            if attribute not in ALLOWED_ATTRIBUTES:
                raise ValueError("Disallowed fragment attribute: " + attribute)
        errors = check_css(node.get("style", ""))
        if errors:
            raise ValueError(errors[0])
    expected_links = {a["href"] for a in expected.find_all("a", href=True)}
    actual_links = {a.get("href", "") for a in actual.find_all("a")}
    if actual_links != expected_links:
        raise ValueError("Fragment must preserve every source URL without additions")
    for link in actual.find_all("a"):
        safe_url(link.get("href", ""))
        if link.get("target") != "_blank" or not {"noopener", "noreferrer"}.issubset(set(link.get("rel", []))):
            raise ValueError("Fragment links require target=_blank and rel=noopener noreferrer")
    actual_text = normalize(actual.get_text(" ", strip=True))
    for element in expected.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "li"]):
        content = normalize(element.get_text(" ", strip=True))
        if content and content not in actual_text:
            raise ValueError("Fragment omitted or rewrote source text: " + content[:160])


async def fragmented_llm_package(settings: Settings, llm: LLMClient, markdown: str, *, name: str,
                                 author: str, issue_date: str) -> tuple[EmailPackage, int]:
    """Design lead + nine sections in bounded calls, then assemble one validated email."""
    title, lead, sections, text = markdown_parts(markdown)
    task = ("Restyle the supplied trusted HTML as one polished, responsive email fragment. Preserve every word, "
            "heading and URL exactly. Use only allowed email-safe elements and inline CSS. Every link must retain "
            "target=_blank and rel='noopener noreferrer'. Return JSON with only the html field. Do not return a "
            "full document, scripts, images, forms, style tags, comments or new prose.")
    lead_has_content = bool(BeautifulSoup(lead, "html.parser").get_text(" ", strip=True))
    designed: list[str] = []
    chunks = ([(None, lead)] if lead_has_content else []) + [
        (section["name"], '<h2 style="' + STYLES["h2"] + '">' +
         html_module.escape(section["name"]) + "</h2>" + str(section["markdown_html"]))
        for section in sections]
    for heading, source_html in chunks:
        fragment = await llm.generate(EmailFragment, task,
            {"section": heading or "newsletter introduction", "source_html": source_html,
             "design_system": {"ink": "#1e293b", "body": "#334155", "accent": "#2563eb",
                               "surface": "#f8fafc", "max_width": "600px"}},
            validator=lambda value, source=source_html, name=heading: validate_fragment(value, source, name),
            task_kind="email_fragment", system_rules=HTML_RULES)
        designed.append(fragment.html)
    lead_fragment = designed[0] if lead_has_content else ""
    section_fragments = designed[1:] if lead_has_content else designed
    cards = "".join('<table role="presentation" width="100%" style="width:100%;margin:14px 0;background-color:#f8fafc;"><tr><td style="padding:22px 20px;">'
                    + fragment + "</td></tr></table>" for fragment in section_fragments)
    document = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta name="color-scheme" content="light"><title>' + html_module.escape(title) + '</title></head>'
                '<body style="margin:0;padding:0;background-color:#eef2f6;font-family:Helvetica,Arial,sans-serif;color:#334155;">'
                '<table role="presentation" width="100%" style="width:100%;background-color:#eef2f6;"><tr><td align="center" style="padding:28px 12px;">'
                '<table role="presentation" width="100%" style="width:100%;max-width:600px;background-color:#ffffff;">'
                '<tr><td style="padding:32px 28px;background-color:#1e293b;color:#ffffff;">'
                '<p style="margin:0 0 10px;font-size:12px;letter-spacing:1.6px;color:#f8fafc;">' + html_module.escape(name) + ' · ' + html_module.escape(issue_date) + '</p>'
                '<h1 style="margin:0 0 12px;font-size:30px;line-height:1.2;color:#ffffff;">' + html_module.escape(title) + '</h1>'
                '<p style="margin:0;color:#f8fafc;">By ' + html_module.escape(author) + '</p></td></tr>'
                '<tr><td style="padding:22px 24px;">' + lead_fragment + cards + '</td></tr>'
                '</table></td></tr></table></body></html>')
    return EmailPackage(html=document, text=text), len(chunks)


def eml_package(title: str, package: EmailPackage) -> str:
    message = EmailMessage(policy=SMTP)
    message["Subject"] = title.replace("\r", " ").replace("\n", " ")[:180]
    message.set_content(package.text)
    message.add_alternative(package.html, subtype="html")
    return message.as_string()


async def build_email(settings: Settings, llm: LLMClient, markdown: str, *, name: str, author: str,
                      issue_date: str, demo: bool = False) -> tuple[EmailPackage, dict]:
    validate_markdown(markdown)
    record = {"input_artifact": "final_draft_newsletter.md", "input_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
              "requested_engine": settings.phase3.mode, "jinja_flag_enabled": settings.features.allow_jinja_fallback,
              "automatic_mock_fallback": False}
    if demo:
        package = demo_package(markdown)
        record["engine"] = "demo_fixture"
    elif settings.phase3.mode == "jinja":
        if not settings.features.allow_jinja_fallback:
            raise LLMError("Jinja is disabled. Enable features.allow_jinja_fallback explicitly to use it.")
        package = template_package(markdown, name=name, author=author, issue_date=issue_date, demo=False)
        record["engine"] = "jinja_explicit"
    else:
        try:
            package, fragment_count = await fragmented_llm_package(
                settings, llm, markdown, name=name, author=author, issue_date=issue_date)
            record.update(engine="llm_fragmented", fragment_count=fragment_count)
        except LLMError as exc:
            if not settings.features.allow_jinja_fallback:
                raise LLMError("Phase 3 failed. Jinja fallback is OFF; no template was substituted. " + str(exc)) from exc
            package = template_package(markdown, name=name, author=author, issue_date=issue_date, demo=False)
            record.update(engine="jinja_opt_in_fallback", llm_failure=str(exc)[:1200])
    record.update(validate_package(package, markdown, content_check=settings.phase3.validate_content_preservation))
    return package, record
