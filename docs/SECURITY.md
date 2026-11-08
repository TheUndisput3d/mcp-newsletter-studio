# Security and privacy

This is a local, single-user application. It is not a hardened multi-tenant SaaS deployment.

## Boundaries

- Default dashboard binds to localhost. Non-loopback CLI binding requires `DASHBOARD_TOKEN`. Use a strong random value and do not expose this server directly to the internet.
- API mutations require an application header and reject mismatched Origin. Trusted hosts are restricted. Optional bearer auth covers APIs. These controls supplement, not replace, host isolation and TLS for remote access.
- Secrets entered in the dashboard are stored in a local `.env` (best-effort mode 0600), not returned through settings/quotas or exported in edition bundles. Python environment variables and local disk are **not an encrypted vault**. Windows permissions must be managed using its filesystem ACLs.
- Key labels and quota groups are public configuration. Do not place secret values in labels, model names or newsletter content.
- Google uses read-only OAuth scopes, state, PKCE, loopback callback and locked token refresh. Each child MCP process receives only its relevant service variables. Model keys are not passed to unrelated MCP adapters.
- Filesystem MCP rejects traversal, absolute paths, disallowed file extensions, symlink traversal and hidden operational files. Application Python code still runs with your OS permissions; this is not full OS sandboxing.
- HTML is validated for safe markup and CSS, then previewed in a sandboxed iframe. External image/style assets, forms, scripts, SVG, tracking and hidden content are rejected. The validator is not a substitute for independent security review.
- Source material and Markdown are untrusted prompt data. Models have no shell/research-tool authority. No generated code is executed. Prompt instructions cannot guarantee absence of hallucination or prompt-injection influence; human review remains mandatory.
- Gmail/Drive documents and private-derived focus are withheld from cloud model calls unless allowed. Public topics are sent to search tools. The selected cloud model receives public evidence and finished Markdown. Do not put private information in public topics or the draft.
- Research bundles may contain editorial metadata, source excerpts and a focus plan derived from private context. Treat local artifacts as sensitive and review them before sharing.
- No automatic email distribution is implemented. Approval records review; it does not publish or send anything.

## Quotas and billing

Shared Groq organization / Gemini project quotas cannot be bypassed by rotating keys. Only genuinely independent authorized groups should be configured separately. Key re-save does not erase cooldowns. Local budgets cannot observe other apps and are not provider-side spending caps. Set provider console limits and review billing yourself. Cancellation does not refund an API request already accepted by the provider.

## Operational guidance

Keep dependencies updated. Store workspace files and OAuth credentials on a trusted disk. Restrict access to backups. Do not commit `.env`, `config.local.yaml`, `secrets/`, runtime databases, personal workspace files or logs. Revoke/rotate leaked tokens. Use narrow container mounts for stronger isolation, while acknowledging that container misconfiguration can still expose host files.

The supplied tests exercise known cases. They do not establish comprehensive penetration testing, audit certification or bug-free operation.
