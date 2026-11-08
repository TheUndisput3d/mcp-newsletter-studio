# Verification report - MCP Newsletter Studio 2.0.0

Date: November 9, 2026. Tested on Linux, Python 3.13.5, Chromium 144.0.7559.96 with Playwright. Observed dependency versions are in `requirements-tested.txt`; this is not a transitive lockfile.

## Final observed results

| Check | Result |
|---|---|
| Unit, security, provider-contract, pipeline and backend tests | **137 passed**, 6 browser tests intentionally skipped in this command |
| Browser-to-running-backend tests | **6 passed**, using the documented transport bridge |
| Python statement coverage | **81.1%** for the non-browser run (not branch coverage or a correctness guarantee) |
| Resource lifecycle | Database handles close after successful and failed transactions; ResourceWarnings treated as errors in final unit run |
| Python compilation | Passed |
| JavaScript syntax | Passed with `node --check` |
| Python wheel build | Passed with `pip wheel --no-build-isolation --no-deps` |
| Wheel contents/imports | Package imports, defaults, template/static files and Phase 3 prompt verified |
| CLI commands | Help/registration and a full offline `run --demo` passed |
| Example artifacts | All 15 manifest hashes match the newly generated v2 synthetic example |

Exact final non-browser command:

```bash
python -m pytest --cov=newsletter_mcp --cov-report=term-missing \
  --cov-report=json:coverage.json --junitxml=tests.xml -q -W error::ResourceWarning
```

Observed non-browser duration: 214.49 seconds. The suite includes parametrized cases; “passed” is pytest's test-case count, not a count of manually reviewed features.

Browser command in this restricted build environment:

```bash
RUN_UI_TESTS=1 UI_TRANSPORT_BRIDGE=1 \
CHROMIUM_EXECUTABLE=/usr/bin/chromium \
python -m pytest tests/e2e -q --junitxml=browser-tests.xml
```

Observed browser duration: 104.76 seconds. In an ordinary local environment omit `UI_TRANSPORT_BRIDGE` and use `python -m playwright install chromium` first. The included CI definition configures a normal direct-browser run, but that CI job was not executed here.

## What the tests cover

**Nine MCP servers:** real subprocess startup, initialization, tool discovery, invocation and teardown. The complete offline workflow exercises all nine adapters. Provider contracts use mocked HTTP; no live accounts are implied.

**Saved-file stage boundaries:** five research Markdown files are written in Stage 1, all five are re-read through Filesystem MCP in Stage 2, and the exact saved final Markdown is re-read for Stage 3. The live code path is exercised with synthetic provider/model responses in integration tests. Missing, failed and empty Product Hunt/X channels use the configured Tavily MCP fallback with provenance. Disabling fallback produces notices, not invented sources.

**Phase 3:** selected LLM path by default; Jinja forbidden when its flag is off; explicit Jinja selection and exhausted-LLM recovery only with the flag on. Tests check preserved paragraphs/list items, ordered headings, source links, text conventions, dangerous HTML/CSS rejection, incomplete responses, output hashes, and unsent multipart email packaging. A failed design retains the draft; Phase-3-only retry does not repeat research. Demo output is explicitly a fixture, not proof of real model generation.

**Providers and key pools:** all six routing modes; native Ollama, Groq, Gemini, OpenAI and Claude request/response contracts; privacy gate; transient errors, invalid credentials, truncation, schema retries, call budgets and task deadlines. More than five keys, duplicate keys, usage-based selection, independent authorized groups, shared 429 cooldowns, persistence across restart, request/token/concurrency limits, reservations, reconciliation, and oversized single-request rejection are tested. The implementation does not claim unlimited free quota.

**Backend and review:** real asynchronous job endpoints, status/events, busy guards, cancellation, interrupted-run recovery, persisted settings, secret redaction, credential save/clear, permission toggles, sandboxed/plain-text artifact delivery, actual ZIP exports, Markdown edits and stale-export rejection. Demo approval is blocked. Positive editorial approval and hash-tamper rejection are tested using controlled synthetic live-branch fixtures, not a real published edition. Editing/repackaging invalidates approval.

**Security and local operations:** path traversal/symlink restrictions, filename and size allowlists, workspace boundaries, host/origin/CSRF guards, optional bearer authentication, provider-scoped subprocess environments, withheld private cloud context, OAuth refresh contracts, atomic local writes and scheduling calculations. These are targeted tests, not an independent security audit or complete MCP interoperability certification.

**Frontend:** navigation, all six mode preferences, settings validation, Jinja gate, key pools, credentials/consents, all nine MCP cards, connection checks, Tavily fallback preferences, generation, progress, cancellation, five review tabs, all research files, Markdown editing, design retry, actual HTML/TXT/EML/ZIP downloads, error recovery, demo approval guard, mobile navigation and horizontal-overflow checks. No uncaught page errors were observed in the final browser run. The positive approval handler is verified separately at pipeline/API boundaries, not with live cloud content in the browser.

## Browser transport limitation

Native browser navigation to localhost was blocked by this environment (`ERR_BLOCKED_BY_ADMINISTRATOR`). No browser security policy was disabled. The test harness therefore loads the **actual application HTML/CSS/JavaScript** and forwards its browser fetch requests through a Python binding to a **real Uvicorn/FastAPI server**. HTTP methods, request bodies, authentication, response bytes/status and application business logic are real. The backend runs the real MCP subprocesses, state store and offline workflow; responses are not replaced with canned API fixtures.

This verifies frontend/backend interaction with a substituted transport. It does **not** establish that browser-native origin/CSP/network enforcement works in this environment. Separate HTTP tests cover server-side protections. The test harness also contains a direct-navigation mode for local use.

Screenshots from the tests: [desktop](dashboard-preview.png), [edition review](review-preview.png), [mobile](dashboard-mobile-preview.png). Detailed browser inventory: `browser-check-results.json`.

## Issues found and corrected during verification

The testing process caught and corrected a malformed engine-selector option, button state restoration after async actions, a screenshot captured mid-sidebar transition, and SQLite connections that committed transactions without closing handles. Database closure now has explicit regression tests, including rollback on exceptions. These fixes are not a claim that no undiscovered issues remain.

## Not verified here

Live Ollama inference/hardware performance; live Groq/Gemini/OpenAI/Claude inference, account permissions, current model entitlements and quota sufficiency; authenticated Tavily, Product Hunt, X, GitHub and Google API access; the user's Google browser-consent flow; Tavily's hosted MCP deployment; a fresh dependency install over the internet; Docker build/run; Windows/macOS runtime execution; direct browser networking in this sandbox; rendering in actual Gmail, Outlook or Apple Mail; newsletter delivery; semantic accuracy of generated news; or arbitrary third-party MCP hosts.

No email was sent. The supplied example is synthetic and cannot be approved by the normal application flow. Add your own authorized credentials and review a live edition before relying on the application for publication. This is a tested local software project, not a guarantee of bug-free operation or completion under arbitrary free-tier quotas.
