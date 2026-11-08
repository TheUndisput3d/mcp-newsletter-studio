# Optional Docker deployment

Native Python is the simplest path for Desktop output and browser OAuth. Docker files are included but the build environment did not have a Docker daemon, so an actual image build/container run was not verified here.

1. Install Docker/Compose. Put a strong random `DASHBOARD_TOKEN` in the host `.env` (for example generate one with Python's `secrets.token_urlsafe(32)`).
2. Run `docker compose up --build -d` and open `http://127.0.0.1:8000`.
3. Enter that dashboard token. Add model/MCP keys using Settings & keys. API keys are saved in `/data/.env`, preferences in `/data/config.local.yaml` and artifacts in `/data/workspace` inside the persistent named volume. The host `.env` is not automatically imported as provider credentials.

The process runs as an unprivileged user. The named volume is initialized with a writable data directory. Do not delete it without a backup: it contains private credentials and research.

To run Ollama as the optional Compose service, set `DOCKER_OLLAMA_BASE_URL=http://ollama:11434` in the host `.env`, then:

```bash
docker compose --profile local-model up --build -d
docker compose exec ollama ollama pull qwen3:8b
```

For host Ollama, the host server must be reachable from the Docker network. A host-only loopback listener may not be reachable from a container. Avoid exposing Ollama publicly; prefer the optional internal service or deliberate firewall/network configuration.

For Google OAuth, create a **Desktop app** OAuth client (recommended for this local loopback flow),
or a Web application client with `http://127.0.0.1:8000/api/oauth/callback/google` as an authorized
redirect URI. Save it at `secrets/client_secret.json` on the host. Compose mounts that one client file read-only at
`/run/secrets/google_client.json`. Open **Settings & keys** and click **Connect Google Workspace**.
The resulting refresh token stays in the persistent `/data/secrets/mcp_oauth.json` file; it is never
baked into the image. The three local Google MCP subprocesses receive only refreshed short-lived
access tokens.

To map outputs to your Desktop, add a narrowly scoped bind mount to the studio service, for example:

```yaml
volumes:
  - studio-data:/data
  - /YOUR/ACTUAL/Desktop/AI-Newsletter:/data/workspace
```

The host directory must be writable by the container user (UID 10001 on Linux). On Docker Desktop, allow the directory through its file-sharing settings. Do not mount your entire home folder. Windows YAML paths may need forward slashes and quoting.

Stop with `docker compose down`; named volumes persist. `docker compose down -v` deletes them and their secrets/artifacts. Pin the Ollama image digest for reproducible deployments. Do not expose this local single-user app directly to the internet.
