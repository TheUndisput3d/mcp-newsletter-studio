from fastapi.testclient import TestClient

from newsletter_mcp.webapp import create_app


def test_dashboard_static_and_safe_config(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert "MCP Newsletter Studio" in client.get("/").text
        response = client.get("/api/config")
        assert len(response.json()["integrations"]) == 9
        assert "Content-Security-Policy" in response.headers
        assert "client_secret" not in response.text
        assert client.get("/static/studio.js").status_code == 200


def test_mutations_require_csrf_header_and_same_origin(settings):
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/runs", json={"demo": True}).status_code == 403
        response = client.post("/api/runs", json={"demo": True}, headers={"X-Requested-With": "newsletter-studio", "Origin": "https://evil.example"})
        assert response.status_code == 403
        assert client.get("/api/config", headers={"Host": "evil.example"}).status_code == 400


def test_dashboard_auth_and_secret_redaction(settings, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-dashboard-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "test-api-secret")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/config").status_code == 401
        response = client.get("/api/config", headers={"Authorization": "Bearer test-dashboard-secret"})
        assert response.status_code == 200
        assert "test-api-secret" not in response.text
        assert "test-dashboard-secret" not in response.text


def test_artifact_routes_do_not_expose_arbitrary_files(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/runs/20261108T010203-12345678/files/.env").status_code == 404
        assert client.get("/api/runs/not-a-run/files/newsletter.html").status_code == 400
