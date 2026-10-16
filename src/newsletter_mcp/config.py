from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SERVER_NAMES = (
    "tavily", "arxiv", "github", "producthunt", "google_drive", "gmail",
    "google_calendar", "filesystem", "twitter",
)


def env_true(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewsletterConfig(StrictModel):
    name: str = "The AI Dispatch"
    audience: str = "Developers and technically curious AI practitioners"
    author: str = "Editorial Team"
    timezone: str = "Asia/Kathmandu"
    lookback_days: int = Field(7, ge=1, le=30)
    topics: list[str] = Field(default_factory=lambda: ["open-source AI", "AI developer tools"])
    workspace: str = "workspace"
    max_items_per_channel: int = Field(6, ge=1, le=20)
    max_search_calls: int = Field(3, ge=1, le=10)
    max_llm_calls: int = Field(30, ge=1, le=100)
    max_input_characters: int = Field(100000, ge=10000, le=300000)
    max_output_tokens: int = Field(7000, ge=1024, le=16000)
    require_web_sources: bool = True
    include_undated_sources: bool = False
    allow_cloud_context: bool = False
    calendar_query: str = "AI Newsletter"
    calendar_id: str = "primary"
    drive_folder_id: str = ""
    drive_document_names: list[str] = Field(default_factory=lambda: [
        "AI Newsletter Performance Data", "AI Newsletter Content Ideas", "Sample Newsletter Template"])
    gmail_query: str = 'newer_than:30d {"newsletter feedback" "content suggestion"}'
    arxiv_query: str = "cat:cs.AI OR cat:cs.LG OR cat:cs.CL"
    github_query: str = "topic:artificial-intelligence archived:false fork:false stars:>100"
    producthunt_topic: str = "artificial-intelligence"
    twitter_query: str = '(AI OR "machine learning") lang:en -is:retweet'

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("Unknown timezone; use an IANA name such as Asia/Kathmandu") from exc
        return value


# Explicit task routing; never silently switch to a different provider.
LLM_MODES = ("ollama", "groq", "hybrid", "gemini", "openai", "claude")
LLMMode = Literal["ollama", "groq", "hybrid", "gemini", "openai", "claude"]


class PoolKeyConfig(StrictModel):
    key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,100}$")
    label: str = Field(default="", max_length=80)
    quota_group: str = Field(default="default", pattern=r"^[a-zA-Z0-9_.-]{1,80}$")


class QuotaConfig(StrictModel):
    # Zero = unknown, not unlimited. Provider responses still govern actual quota.
    rpm: int = Field(0, ge=0)
    tpm: int = Field(0, ge=0)
    rpd: int = Field(0, ge=0)
    tpd: int = Field(0, ge=0)
    max_concurrency: int = Field(1, ge=1, le=20)


class LLMConfig(StrictModel):
    provider: LLMMode = "ollama"
    ollama_model: str = "qwen3:8b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    # A 32k KV cache forces Qwen3 8B to spill heavily onto an 8 GB GPU.  The
    # staged writer keeps prompts small enough that 16k retains the full draft
    # output budget while improving local inference reliability.
    ollama_num_ctx: int = Field(16384, ge=4096, le=131072)
    groq_model: str = "openai/gpt-oss-120b"
    gemini_model: str = "gemini-3.8-flash"
    openai_model: str = "gpt-5-mini"
    claude_model: str = "claude-sonnet-4-6"
    # Retained only for a clear migration error: provider fallback is no longer implicit.
    fallback_provider: Literal[""] = ""
    timeout_seconds: int = Field(300, ge=10, le=3600)
    heavy_timeout_seconds: int = Field(900, ge=10, le=3600)
    task_deadline_seconds: int = Field(2400, ge=30, le=14400)
    request_retries: int = Field(4, ge=0, le=10)
    validation_attempts: int = Field(3, ge=1, le=6)
    max_rate_wait_seconds: int = Field(180, ge=0, le=3600)
    temperature: float = Field(0.2, ge=0, le=1)
    small_task_max_tokens: int = Field(1200, ge=256, le=4000)
    key_pools: dict[str, list[PoolKeyConfig]] = Field(default_factory=dict)
    # Keys use "provider/group", e.g. "groq/default" or "gemini/my-project".
    quotas: dict[str, QuotaConfig] = Field(default_factory=dict)

    @field_validator("key_pools")
    @classmethod
    def valid_pools(cls, pools):
        if set(pools) - {"groq", "gemini", "openai", "claude"}:
            raise ValueError("Pools are supported for groq, gemini, openai and claude")
        for entries in pools.values():
            envs = [entry.key_env for entry in entries]
            if len(envs) != len(set(envs)):
                raise ValueError("Duplicate key environment references in pool")
        return pools

    @field_validator("ollama_base_url")
    @classmethod
    def valid_base(cls, value):
        from urllib.parse import urlsplit
        u = urlsplit(value)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Ollama URL must be an http(s) base URL without credentials or query")
        return value.rstrip("/")


class ResearchFallbackConfig(StrictModel):
    producthunt_to_tavily: bool = True
    twitter_to_tavily: bool = True
    # Public launch/community pages often have no reliable date; label them explicitly.
    allow_undated: bool = True
    max_searches_per_channel: int = Field(1, ge=1, le=3)


class FeatureConfig(StrictModel):
    allow_jinja_fallback: bool = False


class Phase3Config(StrictModel):
    mode: Literal["llm", "jinja"] = "llm"
    max_output_tokens: int = Field(16000, ge=2048, le=64000)
    # Inline CSS, no executable content, and preservation of the saved Markdown.
    validate_content_preservation: bool = True



class MCPConfig(StrictModel):
    """A declared connection to an external MCP server.

    Logical operation names are the newsletter's stable contract.  They are
    mapped to names advertised by the configured server. Provider MCPs may be
    remote vendor/community services or narrowly scoped local subprocesses.
    """
    enabled: bool = True
    required: bool = False
    transport: Literal["stdio", "streamable_http"] = "stdio"
    authentication: Literal["none", "bearer", "oauth"] = "none"
    url: str = ""
    token_env: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    operations: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(90, ge=5, le=600)

    @model_validator(mode="after")
    def valid_connection(self):
        if self.enabled:
            if self.transport == "streamable_http" and not self.url.startswith("https://"):
                raise ValueError("Remote MCP servers require an HTTPS url")
            if self.transport == "stdio" and not self.command:
                raise ValueError("stdio MCP servers require a command")
            if self.authentication in {"bearer", "oauth"} and not self.token_env:
                raise ValueError("authenticated MCP servers require token_env")
        return self


class ScheduleConfig(StrictModel):
    weekday: int = Field(6, ge=0, le=6)
    hour: int = Field(8, ge=0, le=23)
    minute: int = Field(0, ge=0, le=59)


class Settings(StrictModel):
    newsletter: NewsletterConfig = Field(default_factory=NewsletterConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    mcps: dict[str, MCPConfig] = Field(default_factory=lambda: {
        **{n: MCPConfig(enabled=False) for n in SERVER_NAMES if n != "filesystem"},
        "filesystem": MCPConfig(enabled=True, required=True, command="npx",
                                args=["-y", "@modelcontextprotocol/server-filesystem@2025.11.25", "${workspace}"],
                                operations={"read_text": "read_text_file", "write_text": "write_file",
                                            "create_directory": "create_directory"}),
    })
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    research_fallback: ResearchFallbackConfig = Field(default_factory=ResearchFallbackConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    phase3: Phase3Config = Field(default_factory=Phase3Config)
    config_path: Path | None = Field(default=None, exclude=True)
    override_path: Path | None = Field(default=None, exclude=True)
    project_root: Path = Field(default_factory=Path.cwd, exclude=True)

    @property
    def workspace(self) -> Path:
        return (self.project_root / Path(self.newsletter.workspace).expanduser()).resolve()

    @model_validator(mode="after")
    def validate_feature_flags(self):
        if self.phase3.mode == "jinja" and not self.features.allow_jinja_fallback:
            raise ValueError("Jinja requires features.allow_jinja_fallback=true. It is disabled by default.")
        return self

    @field_validator("mcps")
    @classmethod
    def valid_servers(cls, value: dict[str, MCPConfig]) -> dict[str, MCPConfig]:
        unknown = set(value) - set(SERVER_NAMES)
        missing = set(SERVER_NAMES) - set(value)
        if unknown or missing:
            raise ValueError(f"Exactly nine server entries required. Unknown={unknown}; missing={missing}")
        if not value["filesystem"].enabled:
            raise ValueError("Filesystem MCP must be enabled")
        return value


def load_settings(path: str | Path = "config.yaml") -> Settings:
    path = Path(path).resolve()
    secrets_file = os.getenv("NEWSLETTER_SECRETS_FILE")
    load_dotenv(Path(secrets_file) if secrets_file else path.parent / ".env", override=False)
    if not path.exists():
        raise FileNotFoundError(f"Configuration not found: {path}")
    config = Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    config.project_root = path.parent
    config.config_path = path
    for env, field in {
        "LLM_PROVIDER": "provider", "OLLAMA_MODEL": "ollama_model",
        "OLLAMA_BASE_URL": "ollama_base_url", "OLLAMA_NUM_CTX": "ollama_num_ctx", "GROQ_MODEL": "groq_model",
        "LLM_FALLBACK_PROVIDER": "fallback_provider",
        "GEMINI_MODEL": "gemini_model", "OPENAI_MODEL": "openai_model", "CLAUDE_MODEL": "claude_model",
    }.items():
        if os.getenv(env):
            setattr(config.llm, field, os.environ[env])
    # Dashboard preferences override ordinary defaults; secrets are never stored here.
    configured_override = os.getenv("NEWSLETTER_SETTINGS_FILE")
    override_path = Path(configured_override).resolve() if configured_override else path.with_name(path.stem + ".local.yaml")
    if override_path.exists():
        overridden = yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}
        config = Settings.model_validate(deep_merge(config.model_dump(), preference_overlay(overridden)))
        config.project_root, config.config_path, config.override_path = path.parent, path, override_path
    # Deployment routing and the cloud-context privacy gate are authoritative.
    # A stale dashboard snapshot must never redirect a container away from the
    # endpoint selected by its runtime environment or weaken its privacy policy.
    if os.getenv("OLLAMA_BASE_URL"):
        config.llm.ollama_base_url = os.environ["OLLAMA_BASE_URL"]
    if "ALLOW_CLOUD_CONTEXT" in os.environ:
        config.newsletter.allow_cloud_context = env_true("ALLOW_CLOUD_CONTEXT")
    if os.getenv("NEWSLETTER_WORKSPACE"):
        config.newsletter.workspace = os.environ["NEWSLETTER_WORKSPACE"]
    # Revalidate environment values rather than trusting them.
    validated = Settings.model_validate(config.model_dump())
    validated.project_root, validated.config_path, validated.override_path = path.parent, path, override_path
    return validated


def deep_merge(base: dict, changes: dict) -> dict:
    result = dict(base)
    for key, value in changes.items():
        result[key] = deep_merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def preference_overlay(value: dict) -> dict:
    """Keep only user preferences; provider contracts remain image-owned."""
    if not isinstance(value, dict):
        return {}
    models = {
        "newsletter": NewsletterConfig,
        "llm": LLMConfig,
        "schedule": ScheduleConfig,
        "research_fallback": ResearchFallbackConfig,
        "features": FeatureConfig,
        "phase3": Phase3Config,
    }
    result: dict = {}
    for section, model in models.items():
        raw = value.get(section)
        if isinstance(raw, dict):
            result[section] = {key: item for key, item in raw.items() if key in model.model_fields}
    raw_mcps = value.get("mcps")
    if isinstance(raw_mcps, dict):
        result["mcps"] = {
            name: {"enabled": bool(connection["enabled"])}
            for name, connection in raw_mcps.items()
            if name in SERVER_NAMES and isinstance(connection, dict) and "enabled" in connection
        }
    return result


def save_settings(settings: Settings) -> None:
    """Persist only user preferences, never image-owned provider contracts."""
    import tempfile
    path = settings.config_path or settings.project_root / "config.yaml"
    target = settings.override_path or path.with_name(path.stem + ".local.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(preference_overlay(settings.model_dump(mode="json")), f, sort_keys=False, allow_unicode=True)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
