import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from newsletter_mcp.config import Settings
from newsletter_mcp.pipeline import PipelineError, approve_run, next_schedule, read_artifact, repackage_run, run_pipeline
from newsletter_mcp.state import StateStore


@pytest.fixture(scope="module")
def completed_demo(tmp_path_factory):
    cfg = Settings()
    cfg.project_root = Path(__file__).resolve().parents[1]
    cfg.newsletter.workspace = str(tmp_path_factory.mktemp("full-demo"))
    result = asyncio.run(run_pipeline(cfg, demo=True))
    return cfg, result


async def test_complete_pipeline_has_every_required_artifact(completed_demo):
    cfg, result = completed_demo
    assert result["status"] == "awaiting_review"
    assert set(result["mcp_status"]) == {"tavily", "arxiv", "github", "producthunt", "google_drive", "gmail", "google_calendar", "filesystem", "twitter"}
    expected = {"web_research.md", "arxiv_research.md", "github_research.md", "product_hunt_research.md", "twitter_research.md", "final_draft_newsletter.md", "newsletter.html", "newsletter.txt", "newsletter.eml", "research.json", "draft.json", "quality_report.json"}
    assert expected.issubset(result["files"])
    html = await read_artifact(cfg, result["run_id"], "newsletter.html")
    assert html.count("<h2 ") == 9
    assert "SYNTHETIC" in html.upper()
    assert "<script" not in html
    assert "max-width:600px" in html
    assert result["llm_audit"] == []
    assert StateStore(cfg.workspace).get(result["run_id"])["status"] == "awaiting_review"


async def test_artifact_hashes_match_including_eml(completed_demo):
    cfg, result = completed_demo
    for filename, expected in result["files"].items():
        text = await read_artifact(cfg, result["run_id"], filename)
        assert hashlib.sha256(text.encode()).hexdigest() == expected, filename


async def test_demo_cannot_be_approved(completed_demo):
    cfg, result = completed_demo
    with pytest.raises(PipelineError, match="Synthetic"):
        await approve_run(cfg, result["run_id"], "Reviewer")


async def test_repackage_resets_review_state(completed_demo):
    cfg, result = completed_demo
    changed = await repackage_run(cfg, result["run_id"])
    assert changed["status"] == "awaiting_review"
    assert changed["prior_approval_invalidated"]
    approval = json.loads(await read_artifact(cfg, result["run_id"], "approval.json"))
    assert not approval["valid"]


def test_schedule_uses_kathmandu_timezone(settings):
    # Sunday 2026-11-08, 01:00 UTC = 06:45 in Kathmandu. Scheduled 08:00 local.
    due = next_schedule(settings, datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc))
    assert due.isoformat() == "2026-11-08T08:00:00+05:45"
    next_week = next_schedule(settings, datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc))
    assert next_week.day == 15


def test_star_delta_requires_prior_observation(settings):
    store = StateStore(settings.workspace)
    first = store.observe_stars("https://example.com/repo", "2026-11-01T00:00:00Z", 100)
    second = store.observe_stars("https://example.com/repo", "2026-11-08T00:00:00Z", 135)
    assert first["star_change_since_previous_observation"] is None
    assert second["star_change_since_previous_observation"] == 35
    assert second["previous_observed_at"] == "2026-11-01T00:00:00Z"


async def test_missing_required_mcp_fails_without_fabrication(settings):
    settings.mcps["tavily"].required = True
    settings.mcps["tavily"].enabled = False
    with pytest.raises(PipelineError, match="Required MCP tavily"):
        await run_pipeline(settings)
    rows = StateStore(settings.workspace).list_runs()
    assert rows[0]["status"] == "failed"
    manifest = json.loads(await read_artifact(settings, rows[0]["id"], "manifest.json"))
    assert manifest["status"] == "failed"
    assert not manifest["demo"]
