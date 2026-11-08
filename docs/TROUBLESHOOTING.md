# Troubleshooting v2

## First diagnostic

Run `newsletter run --demo`. This makes no external model/provider calls but starts all nine MCP servers and writes the full artifact set. Then run `newsletter doctor` for live credential presence and MCP discovery. A successful `doctor` is not proof that the external API token has the required scopes or spending capacity.

## Dashboard will not connect

Keep `newsletter serve` running. Open the printed localhost address. Use Refresh. A configured dashboard token is requested in a login dialog and retained only in page memory. Another program may own port 8000: run `newsletter serve --port 8001`. Do not use public hostnames without deliberately configuring allowed hosts and authentication.

## Settings appear to be ignored

UI changes are in `config.local.yaml` and take precedence over distributed `config.yaml` and model environment values. Inspect that file. Removing it resets non-secret UI preferences; it does not erase your `.env`. Restart the foreground scheduler after changes. Changing the output workspace switches archives rather than migrating editions.

## No local model / connection refused

Start Ollama and pull the exact configured model. Check Ollama base URL, OS firewall, model memory needs and local context size. In Docker, localhost refers to the container, not the host. All-Ollama and hybrid modes require local inference; they do not silently switch to Groq when Ollama is unavailable.

## 401 / 403 from a model

Check the key, provider and model entitlement. The affected credential is marked unhealthy; healthy credentials may be tried. Replace/re-save credentials after fixing authorization. That clears authorization failures only, not shared quota cooldowns.

## 429 / waiting for quota

Keys from the same Groq organization or Gemini project share limits. Check the provider console, local group budgets and cooldown display. Do not assign fake groups to keys that share quotas. Reduce request size/output budgets or use an authorized larger quota or all-Ollama. A single overlarge request cannot be distributed over keys. No finite retry policy can guarantee completion after a daily quota has been exhausted.

## Phase 3 fails

The saved research and Markdown remain available. Inspect Activity and the manifest error. The model may have returned truncated JSON, rewritten a paragraph, dropped a list item/source link, added unsafe HTML, or exceeded its token quota. Use a capable model with sufficient context/output capacity. Shorten the Markdown while keeping the nine headings, or adjust limits; retry with **Retry Phase 3** / `newsletter render RUN_ID`.

Jinja is deliberately off. To use the retained template, explicitly turn on Allow Jinja template fallback in Settings. Selecting the Jinja engine also requires this flag. The report records whether the actual output came from the LLM, opt-in fallback, explicit template or offline fixture.

## Product Hunt / X are missing

The primary MCP requires credentials and any required use permission. X also needs enablement plus paid-service opt-in. Missing/disabled/failed/empty primary channels use Tavily when the relevant fallback flag is on. Tavily needs its own credentials/available quota. Public search may not find enough dated content; absence is recorded, not fabricated. The fallback never promises authenticated X metrics or access to protected posts.

## Google OAuth fails

Enable Google Drive API, Gmail API and Google Calendar API. Use an OAuth **Desktop app** client for the local loopback flow, or a Web application client with `http://127.0.0.1:8000/api/oauth/callback/google` as an exact authorized redirect URI. Configure the consent screen and add your account as a test user where applicable. Save the JSON as `secrets/client_secret.json`, rebuild/recreate the container so the read-only mount is present, then click **Connect Google Workspace** in Settings & keys. Tokens issued while an application is in testing can expire and require reconnection.

A `403` after a successful connection normally means one of the three APIs is disabled or the consent grant lacks its read-only scope. Disconnect Google, enable all three APIs, and connect again. The project does not call Google's developer-preview MCP endpoints.

## Approval / downloads are disabled

Demo editions cannot be approved. Failed/incomplete editions or manually edited drafts cannot expose current HTML/TXT/EML until Phase 3 completes. A bundle can still contain preserved research and the draft. Any changed artifact hash invalidates previous review. Approval never sends email.

## Restart / cancel behavior

Keep the server running during work. On restart, unfinished jobs are marked interrupted. Completed stage files remain. Phase 3 can be retried when the final Markdown exists; stages that did not complete need a new run. Cancelling cannot cancel billing for an external request already accepted.

## Email looks different in Outlook/Gmail

Table layout and inline CSS are requested and validated, but cross-client visual compatibility was not tested against live inboxes. Preview in your actual email platform, test in target clients and review accessibility and unsubscribe/sender settings before distributing.

## Browser-test transport warning

Some build environments prohibit native browser navigation to localhost. The optional UI test bridge sends real requests to the running backend through a Python binding; it does not replace API responses with fixtures. Run the normal direct-browser tests on your computer to verify native networking/CSP behavior too.
