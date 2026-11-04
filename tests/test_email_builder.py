"""Phase 3 source-of-truth, safety and opt-in fallback tests (no paid calls)."""
from unittest.mock import AsyncMock
import hashlib

import pytest
from pydantic import ValidationError

from newsletter_mcp.config import Settings
from newsletter_mcp.email_builder import (EmailFragment, build_email, demo_package, template_package, validate_markdown,
                                         validate_package, check_css, eml_package)
from newsletter_mcp.llm import LLMError
from newsletter_mcp.render import SECTION_NAMES

@pytest.fixture
def markdown():
    return '# An evidence-led edition\n\n' + '\n\n'.join(
        f'## {name}\n\nParagraph {i}: preserve this **exact wording**.\n\n- Bullet {i} matters.\n\n[Evidence {i}](https://example.com/source/{i})'
        for i, name in enumerate(SECTION_NAMES, 1)) + '\n'

async def test_default_phase3_calls_bounded_llm_fragments(settings, markdown, monkeypatch):
    client = AsyncMock()
    async def generate(schema, task, context, **kwargs):
        assert schema is EmailFragment
        result = EmailFragment(html=context['source_html'])
        kwargs['validator'](result)
        return result
    client.generate.side_effect = generate
    def forbidden(*args, **kwargs): raise AssertionError('Jinja was invoked without opt-in')
    monkeypatch.setattr('newsletter_mcp.email_builder.template_package', forbidden)
    package, report = await build_email(settings, client, markdown, name='N', author='A', issue_date='2026-11-09')
    assert client.generate.call_count == 9
    for call in client.generate.call_args_list:
        assert 'source_html' in call.args[2]
        assert call.kwargs['task_kind'] == 'email_fragment'
        assert 'Email Template Builder Agent' in call.kwargs['system_rules']
    assert report['engine'] == 'llm_fragmented'
    assert report['fragment_count'] == 9
    assert report['input_sha256'] == hashlib.sha256(markdown.encode()).hexdigest()
    assert not report['jinja_flag_enabled']

async def test_llm_failure_does_not_trigger_template_or_mock(settings, markdown, monkeypatch):
    client = AsyncMock(); client.generate.side_effect = LLMError('quota reached')
    forbidden = AsyncMock(side_effect=AssertionError('must not be called'))
    monkeypatch.setattr('newsletter_mcp.email_builder.template_package', forbidden)
    monkeypatch.setattr('newsletter_mcp.email_builder.demo_package', forbidden)
    with pytest.raises(LLMError, match='fallback is OFF'):
        await build_email(settings, client, markdown, name='N', author='A', issue_date='2026-11-09')
    forbidden.assert_not_called()

async def test_flag_allows_fallback_and_records_actual_engine(settings, markdown):
    settings.features.allow_jinja_fallback = True
    client = AsyncMock(); client.generate.side_effect = LLMError('validation exhausted')
    package, report = await build_email(settings, client, markdown, name='N', author='A', issue_date='2026-11-09')
    assert report['engine'] == 'jinja_opt_in_fallback'
    assert 'validation exhausted' in report['llm_failure']
    assert 'Bullet 9 matters.' in package.html

async def test_explicit_jinja_requires_flag_and_skips_llm(settings, markdown):
    with pytest.raises(ValidationError, match='Jinja requires'):
        Settings.model_validate({'phase3': {'mode': 'jinja'}})
    settings.phase3.mode = 'jinja'
    with pytest.raises(LLMError, match='Jinja is disabled'):
        await build_email(settings, AsyncMock(), markdown, name='N', author='A', issue_date='2026-11-09')
    settings.features.allow_jinja_fallback = True
    client = AsyncMock()
    package, report = await build_email(settings, client, markdown, name='N', author='A', issue_date='2026-11-09')
    assert report['engine'] == 'jinja_explicit'
    client.generate.assert_not_called()

async def test_demo_is_labeled_fixture_not_a_failure_fallback(settings, markdown, monkeypatch):
    client = AsyncMock()
    package, report = await build_email(settings, client, markdown, name='N', author='A', issue_date='2026-11-09', demo=True)
    assert report['engine'] == 'demo_fixture'
    assert not report['automatic_mock_fallback']
    client.generate.assert_not_called()

@pytest.mark.parametrize('malicious', [
    '<script>alert(1)</script>', '<img src="https://evil.example/tracker">', '<iframe src="https://evil.example"></iframe>',
    '<svg onload="alert(1)"></svg>', '<form action="https://evil.example"></form>',
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<div onclick="alert(1)">X</div>', '<style>body{display:none}</style>',
    '<div style="background:url(https://evil.example)">X</div>',
    '<!--[if mso]><img src="https://evil.example"><![endif]-->',
    '<p style="display:none">hidden instructions</p>',
])
def test_html_rejects_active_hidden_or_remote_content(markdown, malicious):
    package = demo_package(markdown)
    package.html = package.html.replace('</body>', malicious + '</body>')
    with pytest.raises(ValueError): validate_package(package, markdown)

@pytest.mark.parametrize('style', ['background:u\\72l(https://evil.example);', 'width:expression(alert(1));', 'opacity:0;', 'visibility:hidden;', '@import "https://evil.example";'])
def test_css_parser_rejects_obfuscation(style):
    assert check_css(style)

@pytest.mark.parametrize('change', ['missing_section', 'changed_url', 'paragraph', 'bullet', 'target', 'plain_url', 'plain_paragraph'])
def test_preserves_all_sections_links_and_editorial_content(markdown, change):
    p = demo_package(markdown)
    if change == 'missing_section': p.html = p.html.replace('Closing Notes', 'Different Notes')
    elif change == 'changed_url': p.html = p.html.replace('https://example.com/source/1', 'https://invented.example/1')
    elif change == 'paragraph': p.html = p.html.replace('Paragraph 1:', 'Something new:')
    elif change == 'bullet': p.html = p.html.replace('Bullet 9 matters.', 'Different assertion.')
    elif change == 'target': p.html = p.html.replace('target="_blank"', '')
    elif change == 'plain_url': p.text = p.text.replace('(https://example.com/source/1)', 'no source')
    elif change == 'plain_paragraph': p.text = p.text.replace('Paragraph 1:', 'Something new:')
    with pytest.raises(ValueError): validate_package(p, markdown)

def test_markdown_no_images_wrong_headings_or_bad_links(markdown):
    for changed in [markdown.replace('Introduction', 'Wrong'), markdown + '\n![tracker](https://evil.example/t.png)']:
        with pytest.raises(ValueError): validate_markdown(changed)

def test_unsent_multipart_email(markdown):
    from email import message_from_string
    message = message_from_string(eml_package('Title\r\nInjected', demo_package(markdown)))
    assert 'To' not in message and 'From' not in message
    assert message.get_content_type() == 'multipart/alternative'
    assert len(message.get_payload()) == 2
