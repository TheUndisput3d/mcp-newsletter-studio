from __future__ import annotations

import asyncio
import hmac
import io
import json
import os
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from newsletter_mcp.config import SERVER_NAMES, LLMMode, LLM_MODES, PoolKeyConfig, Settings, deep_merge, env_true, save_settings
from newsletter_mcp.key_pool import KeyPool, credentials
from newsletter_mcp.llm import LLMClient
from newsletter_mcp.mcp_oauth import begin as begin_mcp_oauth, disconnect as disconnect_mcp_oauth, finish as finish_mcp_oauth, oauth_status
from newsletter_mcp.pipeline import (PipelineError, approve_run, artifact_client, new_run_id, next_schedule,
    read_artifact, repackage_run, run_pipeline, save_markdown, valid_run_id)
from newsletter_mcp.registry import MCPHub
from newsletter_mcp.protocol import MCPError
from newsletter_mcp.secrets_store import update_secrets
from newsletter_mcp.state import StateStore

PUBLIC_FILES = {
    "newsletter.html", "newsletter.txt", "newsletter.eml", "final_draft_newsletter.md", "draft.json",
    "research.json", "web_research.md", "arxiv_research.md", "github_research.md", "product_hunt_research.md",
    "twitter_research.md", "quality_report.json", "manifest.json", "context_summary.json", "focus_plan.json",
    "approval.json", "phase3_report.json",
}
ACTIVE = {"queued", "starting", "context", "focus", "research", "drafting", "designing", "validating", "packaging"}
EXPORT_FILES = {"newsletter.html", "newsletter.txt", "newsletter.eml"}


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(APIModel):
    provider: LLMMode | None = None
    demo: bool = False


class ApprovalRequest(APIModel):
    reviewer: str = Field(min_length=1, max_length=120)


class MarkdownRequest(APIModel):
    markdown: str = Field(min_length=100, max_length=150000)


class SettingsPatch(APIModel):
    newsletter: dict | None = None
    llm: dict | None = None
    mcps: dict | None = None
    schedule: dict | None = None
    research_fallback: dict | None = None
    features: dict | None = None
    phase3: dict | None = None


class CredentialInput(APIModel):
    name: Literal["TAVILY_API_KEY", "GITHUB_TOKEN", "PRODUCTHUNT_MCP_ACCESS_TOKEN", "GOOGLE_DRIVE_MCP_ACCESS_TOKEN", "GOOGLE_GMAIL_MCP_ACCESS_TOKEN", "GOOGLE_CALENDAR_MCP_ACCESS_TOKEN", "X_BEARER_TOKEN"]
    value: SecretStr


class PoolKeyInput(APIModel):
    value: SecretStr
    quota_group: str = Field(default="default", pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    label: str = Field(default="", max_length=80)


class PoolInput(APIModel):
    provider: Literal["groq", "gemini", "openai", "claude"]
    keys: list[PoolKeyInput]


class ModelCheck(APIModel):
    provider: LLMMode


class PingResult(APIModel):
    status: Literal["ok"]


def config_summary(settings: Settings) -> dict:
    oauth = oauth_status(settings)
    integrations = []
    for name in SERVER_NAMES:
        config = settings.mcps[name]
        has_token = config.authentication == "none" or bool(os.getenv(config.token_env)) or oauth.get(name, False)
        state = "disabled" if not config.enabled else "configured" if has_token else "needs_setup"
        fallback = name in {"twitter", "producthunt"} and getattr(settings.research_fallback, name + "_to_tavily")
        integrations.append({"name": name, "state": state, "required": config.required,
                             "transport": config.transport, "tavily_fallback": fallback})
    return {"name": settings.newsletter.name, "provider": settings.llm.provider, "modes": list(LLM_MODES),
            "ollama_model": settings.llm.ollama_model, "groq_model": settings.llm.groq_model,
            "timezone": settings.newsletter.timezone, "lookback_days": settings.newsletter.lookback_days,
            "workspace": str(settings.workspace), "integrations": integrations,
            "cloud_context_allowed": settings.newsletter.allow_cloud_context,
            "phase3": settings.phase3.model_dump(), "features": settings.features.model_dump(),
            "auth_required": bool(os.getenv("DASHBOARD_TOKEN")),
            "next_scheduled_slot": next_schedule(settings).isoformat(), "scheduler_running": False,
            "scheduler_note": "The dashboard does not start the scheduler. Run newsletter schedule separately.",
            "oauth": {"producthunt": oauth.get("producthunt", False),
                      "google": all(oauth.get(name, False) for name in ("google_drive", "gmail", "google_calendar"))},
            "key_counts": {p: len(credentials(settings, p)) for p in ("groq", "gemini", "openai", "claude")},
            "credential_presence": {key: bool(os.getenv(key)) for key in ("TAVILY_API_KEY", "GITHUB_TOKEN", "PRODUCTHUNT_MCP_ACCESS_TOKEN", "GOOGLE_DRIVE_MCP_ACCESS_TOKEN", "GOOGLE_GMAIL_MCP_ACCESS_TOKEN", "GOOGLE_CALENDAR_MCP_ACCESS_TOKEN", "X_BEARER_TOKEN")}}


def create_app(settings: Settings) -> FastAPI:
    tasks: dict[str, asyncio.Task] = {}
    static_root = Path(__file__).parent

    @asynccontextmanager
    async def lifespan(app):
        # Recover interrupted jobs only if another process is not actively running this workspace.
        cfg = app.state.settings
        lock = FileLock(str(cfg.workspace / ".pipeline.lock"), timeout=0)
        try:
            cfg.workspace.mkdir(parents=True, exist_ok=True)
            lock.acquire()
            store = StateStore(cfg.workspace)
            for row in store.list_runs(500):
                if row["status"] in ACTIVE:
                    store.update(row["id"], "interrupted", error="Server restarted. Completed stage artifacts were retained.")
        except Timeout:
            pass
        finally:
            if lock.is_locked:
                lock.release()
        yield
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    app = FastAPI(title="MCP Newsletter Studio", version="2.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.tasks = tasks
    app.state.oauth_states = {}
    hosts = ["127.0.0.1", "localhost", "[::1]", "testserver"]
    hosts += [h.strip() for h in os.getenv("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip() and h.strip() != "*"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.mount("/static", StaticFiles(directory=static_root / "static"), name="static")

    def cfg() -> Settings:
        return app.state.settings

    def store() -> StateStore:
        return StateStore(cfg().workspace)

    def busy() -> bool:
        return any(not task.done() for task in tasks.values())

    def require_idle():
        if busy():
            raise HTTPException(409, "A generation or model check is active. Cancel it or wait for completion before changing settings.")
        lock = FileLock(str(cfg().workspace / ".pipeline.lock"), timeout=0)
        try:
            cfg().workspace.mkdir(parents=True, exist_ok=True)
            with lock:
                pass
        except Timeout:
            raise HTTPException(409, "This workspace is busy in another process")

    def enqueue(identifier: str, work):
        async def runner():
            try:
                await work
            except asyncio.CancelledError:
                if store().get(identifier) and store().get(identifier)["status"] in ACTIVE:
                    store().update(identifier, "cancelled", error="Run cancelled")
            except Exception as exc:
                row = store().get(identifier)
                if row and row["status"] in ACTIVE:
                    store().update(identifier, "failed", error=str(exc)[:1000])
        # Keep strong references; shutdown cancels and joins tasks. No unobserved exceptions.
        tasks[identifier] = asyncio.create_task(runner(), name="newsletter-" + identifier)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/oauth/callback/"):
            token = os.getenv("DASHBOARD_TOKEN", "")
            if token and not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
                return JSONResponse({"detail": "Dashboard token required"}, status_code=401)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("X-Requested-With") != "newsletter-studio":
                    return JSONResponse({"detail": "Missing CSRF request header"}, status_code=403)
                origin = request.headers.get("Origin")
                if origin and urlsplit(origin).netloc != request.url.netloc:
                    return JSONResponse({"detail": "Cross-origin mutation blocked"}, status_code=403)
                if int(request.headers.get("Content-Length", "0") or "0") > 1_500_000:
                    return JSONResponse({"detail": "Request body too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return response

    @app.exception_handler(PipelineError)
    async def pipeline_exception(request: Request, exc: PipelineError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_exception(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 response can echo API key values from invalid request bodies.
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]}, status_code=422)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (static_root / "templates" / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": "2.0.0", "busy": busy()}

    @app.get("/api/config")
    async def config():
        return config_summary(cfg()) | {"busy": busy()}

    @app.get("/api/settings")
    async def settings_get():
        return cfg().model_dump(mode="json")

    @app.put("/api/settings")
    async def settings_put(body: SettingsPatch):
        require_idle()
        try:
            new = Settings.model_validate(deep_merge(cfg().model_dump(), body.model_dump(exclude_none=True)))
            new.project_root, new.config_path, new.override_path = cfg().project_root, cfg().config_path, cfg().override_path
            new.workspace.mkdir(parents=True, exist_ok=True)
            save_settings(new)
        except (ValueError, OSError) as exc:
            if isinstance(exc, ValidationError):
                detail = "; ".join(str(e["loc"]) + ": " + e["msg"] for e in exc.errors())
            else:
                detail = str(exc)
            raise HTTPException(422, detail[:2000])
        app.state.settings = new
        return config_summary(new)

    @app.get("/api/quotas")
    async def quotas():
        return KeyPool(cfg()).snapshot()

    @app.post("/api/key-pools")
    async def key_pools(body: PoolInput):
        require_idle()
        prefix = body.provider.upper()
        new = cfg().model_copy(deep=True)
        changes: dict[str, str | None] = {entry.key_env: None for entry in new.llm.key_pools.get(body.provider, [])}
        legacy = "ANTHROPIC" if body.provider == "claude" else prefix
        changes.update({legacy + "_API_KEY": None, legacy + "_API_KEYS": None})
        entries, seen = [], set()
        for index, key in enumerate(body.keys, 1):
            secret = key.value.get_secret_value().strip()
            if not secret or secret in seen:
                continue
            if len(secret) > 10000 or any(c in secret for c in "\r\n"):
                raise HTTPException(422, "Keys must be nonempty, single-line credentials")
            seen.add(secret)
            name = f"{prefix}_POOL_KEY_{index:04d}"
            changes[name] = secret
            entries.append(PoolKeyConfig(key_env=name, label=key.label or f"{body.provider}-key-{index:02d}", quota_group=key.quota_group))
        new.llm.key_pools[body.provider] = entries
        update_secrets(new.project_root, changes)
        save_settings(new)
        app.state.settings = new
        KeyPool(new).reset_authorization_failures(body.provider)
        return {"provider": body.provider, "configured_keys": len(entries), "quota_reset": False}

    @app.post("/api/credentials")
    async def credential(body: CredentialInput):
        require_idle()
        value = body.value.get_secret_value().strip()
        if len(value) > 10000 or any(c in value for c in "\r\n"):
            raise HTTPException(422, "Invalid credential format")
        update_secrets(cfg().project_root, {body.name: value or None})
        return {"name": body.name, "configured": bool(value)}

    @app.post("/api/oauth/{provider}/start")
    async def oauth_start(provider: Literal["producthunt", "google"], request: Request):
        require_idle()
        redirect_uri = str(request.url_for("oauth_callback", provider=provider))
        try:
            authorization_url, session = await begin_mcp_oauth(cfg(), provider, redirect_uri)
        except (MCPError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(400, str(exc)[:1000]) from exc
        app.state.oauth_states[session["state"]] = session
        # Remove abandoned state/verifier material after ten minutes.
        cutoff = __import__("time").time() - 600
        app.state.oauth_states = {key: value for key, value in app.state.oauth_states.items()
                                  if value.get("created_at", 0) >= cutoff}
        return {"authorization_url": authorization_url}

    @app.get("/api/oauth/callback/{provider}", response_class=HTMLResponse, name="oauth_callback")
    async def oauth_callback(provider: Literal["producthunt", "google"], state: str = "", code: str = "", error: str = ""):
        session = app.state.oauth_states.pop(state, None)
        if error:
            return HTMLResponse("<h1>Authorization declined</h1><p>You may close this tab.</p>", status_code=400)
        if not session or session.get("provider") != provider or not code:
            return HTMLResponse("<h1>Invalid or expired OAuth callback</h1><p>Start the connection again.</p>", status_code=400)
        try:
            await finish_mcp_oauth(cfg(), session, code)
        except Exception as exc:
            return HTMLResponse(f"<h1>Authorization failed</h1><p>{type(exc).__name__}. Return to Settings and retry.</p>", status_code=400)
        return HTMLResponse("<h1>Connected</h1><p>The MCP authorization was saved locally. You can close this tab and refresh the dashboard.</p>")

    @app.post("/api/oauth/{provider}/disconnect")
    async def oauth_disconnect(provider: Literal["producthunt", "google"]):
        require_idle()
        disconnect_mcp_oauth(cfg(), provider)
        return config_summary(cfg())

    @app.get("/api/runs")
    async def runs():
        return store().list_runs()

    @app.post("/api/runs", status_code=202)
    async def start_run(body: RunRequest):
        require_idle()
        identifier = new_run_id()
        current = cfg().model_copy(deep=True)
        mode = body.provider or current.llm.provider
        store().start(identifier, "demo" if body.demo else mode, body.demo)
        store().update(identifier, "queued")
        enqueue(identifier, run_pipeline(current, demo=body.demo, provider=mode, identifier=identifier))
        return {"run_id": identifier, "status": "queued"}

    @app.post("/api/doctor")
    async def doctor():
        async with MCPHub(cfg()) as hub:
            return {"servers": hub.status, "scope": "MCP handshake and credential presence, NOT live provider authorization"}

    @app.post("/api/model-check")
    async def model_check(body: ModelCheck):
        require_idle()
        settings = cfg().model_copy(deep=True)
        settings.llm.provider = body.provider
        settings.llm.small_task_max_tokens = 256
        settings.newsletter.max_output_tokens = 256
        settings.llm.request_retries = min(1, settings.llm.request_retries)
        settings.llm.max_rate_wait_seconds = min(10, settings.llm.max_rate_wait_seconds)
        llm = LLMClient(settings)
        task = asyncio.current_task()
        tasks["model-check"] = task
        try:
            for kind in (["focus", "draft"] if body.provider == "hybrid" else ["diagnostics"]):
                await llm.generate(PingResult, 'Return {"status":"ok"}. This is a user-requested connection test.', {}, task_kind=kind)
            return {"ok": True, "audit": llm.audit}
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)[:1200])
        finally:
            tasks.pop("model-check", None)

    @app.get("/api/runs/{identifier}")
    async def run(identifier: str):
        valid_run_id(identifier)
        result = store().get(identifier)
        if not result:
            raise HTTPException(404, "Run not found")
        return result | {"events": store().events(identifier)}

    @app.post("/api/runs/{identifier}/cancel")
    async def cancel(identifier: str):
        valid_run_id(identifier)
        task = tasks.get(identifier)
        if not task or task.done():
            raise HTTPException(409, "This run is not active in this dashboard process")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return {"run_id": identifier, "status": "cancelled"}

    @app.post("/api/runs/{identifier}/repackage", status_code=202)
    async def repackage(identifier: str, body: RunRequest):
        require_idle()
        valid_run_id(identifier)
        row = store().get(identifier)
        if not row:
            raise HTTPException(404, "Run not found")
        try:
            await read_artifact(cfg(), identifier, "final_draft_newsletter.md")
        except Exception:
            raise HTTPException(409, "No saved Markdown draft exists yet. Start a new run.")
        store().update(identifier, "queued", has_package=False, error="")
        enqueue(identifier, repackage_run(cfg().model_copy(deep=True), identifier, provider=body.provider))
        return {"run_id": identifier, "status": "queued"}

    @app.put("/api/runs/{identifier}/draft")
    async def draft_put(identifier: str, body: MarkdownRequest):
        require_idle()
        if not store().get(identifier):
            raise HTTPException(404, "Run not found")
        return await save_markdown(cfg(), identifier, body.markdown)

    @app.get("/api/runs/{identifier}/files/{filename}")
    async def artifact(identifier: str, filename: str, download: bool = False):
        valid_run_id(identifier)
        if filename not in PUBLIC_FILES:
            raise HTTPException(404, "Artifact not exposed")
        row = store().get(identifier)
        if not row:
            raise HTTPException(404, "Run not found")
        if filename in EXPORT_FILES and row["status"] not in {"awaiting_review", "approved"}:
            raise HTTPException(409, "Exports are stale or incomplete. Complete Phase 3 before previewing or downloading.")
        try:
            content = await read_artifact(cfg(), identifier, filename)
        except Exception:
            raise HTTPException(404, "Artifact is not available yet")
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'} if download else {}
        return Response(content, media_type="text/plain; charset=utf-8", headers=headers)

    @app.get("/api/runs/{identifier}/download")
    async def download(identifier: str):
        valid_run_id(identifier)
        row = store().get(identifier)
        if not row:
            raise HTTPException(404, "Run not found")
        buffer = io.BytesIO()
        async with artifact_client(cfg()) as client:
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for filename in sorted(PUBLIC_FILES):
                    if filename in EXPORT_FILES and row["status"] not in {"awaiting_review", "approved"}:
                        continue
                    try:
                        content = (await client.call("read_text", {"path": f"issues/{identifier}/{filename}"}))["text"]
                        archive.writestr(filename, content)
                    except Exception:
                        continue
        return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="newsletter-{identifier}.zip"'})

    @app.post("/api/runs/{identifier}/approve")
    async def approve(identifier: str, body: ApprovalRequest):
        require_idle()
        return await approve_run(cfg(), identifier, body.reviewer)

    return app
