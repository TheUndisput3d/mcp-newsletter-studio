from pathlib import Path

import os
import pytest

from newsletter_mcp.config import Settings


@pytest.fixture
def settings(tmp_path):
    cfg = Settings()
    cfg.project_root = tmp_path
    cfg.config_path = tmp_path / "config.yaml"
    cfg.newsletter.workspace = str(tmp_path / "workspace")
    return cfg


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("GROQ_", "GEMINI_", "OPENAI_", "ANTHROPIC_", "CLAUDE_", "OLLAMA_", "LLM_", "DASHBOARD_")):
            monkeypatch.delenv(key, raising=False)
    for key in ("NEWSLETTER_DEMO", "TAVILY_API_KEY", "GITHUB_TOKEN", "PRODUCTHUNT_TOKEN", "PRODUCTHUNT_USE_AUTHORIZED",
                "X_BEARER_TOKEN", "GOOGLE_ACCESS_TOKEN",
                "GROQ_API_KEY", "DASHBOARD_TOKEN", "ALLOW_CLOUD_CONTEXT", "NEWSLETTER_SETTINGS_FILE",
                "NEWSLETTER_WORKSPACE", "NEWSLETTER_SECRETS_FILE", "MCP_OAUTH_FILE"):
        monkeypatch.delenv(key, raising=False)
