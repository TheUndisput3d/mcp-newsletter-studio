import json

from newsletter_mcp.mcp_oauth import begin


async def test_google_dashboard_oauth_accepts_desktop_loopback_client(settings):
    secrets = settings.project_root / "secrets"
    secrets.mkdir()
    (secrets / "client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "desktop-id", "client_secret": "desktop-secret",
        "redirect_uris": ["http://localhost"]}}))
    url, session = await begin(settings, "google", "http://127.0.0.1:8000/api/oauth/callback/google")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert session["client_id"] == "desktop-id"
    assert session["client_secret"] == "desktop-secret"
    assert session["redirect_uri"] == "http://127.0.0.1:8000/api/oauth/callback/google"
