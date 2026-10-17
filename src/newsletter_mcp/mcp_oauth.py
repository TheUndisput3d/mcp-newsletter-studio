"""OAuth sessions for provider connections.

Product Hunt uses its advertised RFC 7591 registration endpoint and PKCE.
The local Google MCP servers use a standard Google Web OAuth client and normal
Drive, Gmail, and Calendar read-only API scopes with offline refresh tokens.
Secrets stay in a mode-0600 local file and are never returned by API routes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from filelock import FileLock

from newsletter_mcp.config import Settings
from newsletter_mcp.protocol import MCPError

PRODUCT_HUNT_RESOURCE = "https://mcp.producthunt.com/mcp"
PRODUCT_HUNT_METADATA = "https://www.producthunt.com/.well-known/oauth-authorization-server/mcp/oauth"
GOOGLE_AUTHORIZATION = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
SERVER_RECORD = {
    "producthunt": "producthunt",
    "google_drive": "google",
    "gmail": "google",
    "google_calendar": "google",
}


def token_path(settings: Settings) -> Path:
    configured = os.getenv("MCP_OAUTH_FILE")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else settings.project_root / path
    return settings.project_root / "secrets" / "mcp_oauth.json"


def _read(settings: Settings) -> dict:
    path = token_path(settings)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(settings: Settings, value: dict) -> None:
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=20)
    with lock:
        fd, temporary = tempfile.mkstemp(prefix=".mcp-oauth-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def oauth_status(settings: Settings) -> dict[str, bool]:
    records = _read(settings)
    return {name: bool(records.get(record, {}).get("refresh_token") or records.get(record, {}).get("access_token"))
            for name, record in SERVER_RECORD.items()}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _google_client(settings: Settings) -> tuple[str, str]:
    path = settings.project_root / os.getenv("GOOGLE_CREDENTIALS_FILE", "secrets/client_secret.json")
    if not path.is_file():
        raise MCPError("Google OAuth client missing: save it as secrets/client_secret.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    # A local dashboard is an installed application for OAuth purposes and can
    # use Google's loopback redirect. Web clients remain supported when their
    # authorized redirect URI matches the dashboard callback exactly.
    client = data.get("installed") or data.get("web") or {}
    if not client:
        raise MCPError("Google OAuth JSON must contain an installed or web application client")
    if not client.get("client_id") or not client.get("client_secret"):
        raise MCPError("Google OAuth client JSON has no client_id/client_secret")
    return client["client_id"], client["client_secret"]


async def begin(settings: Settings, provider: str, redirect_uri: str) -> tuple[str, dict]:
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    if provider == "producthunt":
        async with httpx.AsyncClient(timeout=30) as client:
            metadata_response = await client.get(PRODUCT_HUNT_METADATA, headers={"Accept": "application/json"})
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            registration = await client.post(metadata["registration_endpoint"], json={
                "client_name": "MCP Newsletter Studio",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            })
            registration.raise_for_status()
            registered = registration.json()
        client_id = registered["client_id"]
        scopes = "mcp:account launches:read"
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                 "scope": scopes, "state": state, "code_challenge": challenge,
                 "code_challenge_method": "S256", "resource": PRODUCT_HUNT_RESOURCE}
        authorization_url = metadata["authorization_endpoint"] + "?" + urlencode(query)
        session = {"provider": provider, "state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                   "client_id": client_id, "token_endpoint": metadata["token_endpoint"],
                   "resource": PRODUCT_HUNT_RESOURCE, "created_at": time.time()}
        return authorization_url, session
    if provider == "google":
        client_id, client_secret = _google_client(settings)
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                 "scope": " ".join(GOOGLE_SCOPES), "state": state, "code_challenge": challenge,
                 "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
        session = {"provider": provider, "state": state, "verifier": verifier, "redirect_uri": redirect_uri,
                   "client_id": client_id, "client_secret": client_secret, "token_endpoint": GOOGLE_TOKEN,
                   "created_at": time.time()}
        return GOOGLE_AUTHORIZATION + "?" + urlencode(query), session
    raise MCPError("Unknown OAuth provider")


async def finish(settings: Settings, session: dict, code: str) -> None:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": session["redirect_uri"],
            "client_id": session["client_id"], "code_verifier": session["verifier"]}
    if session.get("client_secret"):
        form["client_secret"] = session["client_secret"]
    if session.get("resource"):
        form["resource"] = session["resource"]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(session["token_endpoint"], data=form,
                                     headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise MCPError(f"OAuth token exchange failed (HTTP {response.status_code})")
    token = response.json()
    if not token.get("access_token"):
        raise MCPError("OAuth provider returned no access token")
    key = "google" if session["provider"] == "google" else "producthunt"
    records = _read(settings)
    previous = records.get(key, {})
    records[key] = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or previous.get("refresh_token", ""),
        "expires_at": time.time() + int(token.get("expires_in", 3600)),
        "client_id": session["client_id"],
        "client_secret": session.get("client_secret", ""),
        "token_endpoint": session["token_endpoint"],
        "resource": session.get("resource", ""),
    }
    _write(settings, records)


async def access_token(settings: Settings, server: str) -> str:
    key = SERVER_RECORD.get(server)
    if not key:
        raise MCPError(f"No OAuth session is defined for {server}")
    records = _read(settings)
    record = records.get(key, {})
    if record.get("access_token") and record.get("expires_at", 0) > time.time() + 90:
        return record["access_token"]
    if not record.get("refresh_token"):
        raise MCPError("OAuth authorization required; use Settings in the dashboard")
    form = {"grant_type": "refresh_token", "refresh_token": record["refresh_token"],
            "client_id": record["client_id"]}
    if record.get("client_secret"):
        form["client_secret"] = record["client_secret"]
    if record.get("resource"):
        form["resource"] = record["resource"]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(record["token_endpoint"], data=form, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise MCPError(f"OAuth refresh failed (HTTP {response.status_code}); reconnect in Settings")
    fresh = response.json()
    record.update(access_token=fresh["access_token"], expires_at=time.time() + int(fresh.get("expires_in", 3600)))
    if fresh.get("refresh_token"):
        record["refresh_token"] = fresh["refresh_token"]
    records[key] = record
    _write(settings, records)
    return record["access_token"]


def disconnect(settings: Settings, provider: str) -> None:
    records = _read(settings)
    records.pop("google" if provider == "google" else provider, None)
    _write(settings, records)
