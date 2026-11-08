# Architecture and phase boundaries (v2)

## Components

```text
Local browser dashboard / CLI / foreground scheduler
  -> Python MCP host and deterministic task orchestration
     -> task router -> Ollama | Groq | hybrid | Gemini | OpenAI | Claude
        -> quota-aware credential pool for cloud calls
     -> MCP stdio / Tavily optional Streamable HTTP
        -> Tavily, arXiv, GitHub, Product Hunt, X,
           Google Drive, Gmail, Google Calendar, Filesystem
     -> saved research -> saved draft -> LLM HTML/TXT -> validation -> review/export
```

All research and newsletter artifact access crosses MCP. Provider HTTP requests are implementation details inside the adapters. Model inference calls, operational SQLite state and configuration storage do not need MCP. All nine adapters are included; configured/authorized adapters run live, and the offline demo intentionally starts all nine with synthetic provider responses.

This is **one explicit Python workflow with model tasks**, not three autonomous agents that can invent and execute arbitrary tools. MCP tool choice is controlled by application code. The model never receives shell execution or arbitrary browser tools. The protocol implementation is a tested tools-only subset, not a general-purpose MCP SDK or vendor certification.

## Stage 1

1. Initialize configured servers and discover tools; enforce required/paid gates.
2. Fetch requested Drive documents, Gmail feedback and Calendar events through MCP.
3. Run a focus-planning LLM task. Private context is allowed locally; cloud use requires consent.
4. Research five channels. Web queries use explicit public topics, not raw private feedback.
5. Apply optional Tavily fallback to unavailable/empty Product Hunt and X channels.
6. Normalize metadata, deduplicate, apply date policy and record limitations.
7. Save five Markdown research artifacts plus source/context/focus JSON through Filesystem MCP.

Fallback provenance is stored per source (`retrieval_method: tavily_fallback`, `platform_authenticated: false`) and per channel. Undated fallback pages are explicitly marked. These are excerpts, not authenticated engagement metrics. GitHub star growth is only recorded when this workspace has an earlier observation; total stars alone are not growth.

## Stage 2

The host **re-reads all five saved files**, supplies them and a source ledger to the selected drafting LLM, and checks the structured draft. Sources are referenced by known IDs; URLs are resolved from tool records in rendering. Source checking is structural, not independent semantic fact verification.

All nine sections are represented. Missing channels get availability notices. X public-web fallback changes the eighth heading to `AI Community Highlights`. Drive's sample newsletter document is editorial guidance, not an executable template. `draft.json` and the final Markdown are persisted before design begins.

## Stage 3

The host **reads `final_draft_newsletter.md` through Filesystem MCP**. This exact text, its identity/hash and the dedicated Email Template Builder prompt drive the model call. The selected model must create both HTML and plain text; the renderer does not supply a fixed HTML layout in default live mode.

Validation covers the nine sections, source link set, paragraph/list preservation, inline CSS, safe tags/attributes, table-based layout and the requested design conventions. It blocks active content and external/hidden assets; it cannot prove correct visual rendering in every email application.

`features.allow_jinja_fallback=false` prevents template substitution, including on quota, connection, formatting and validation failures. Enabling that flag allows the retained template only after failure or explicit `phase3.mode=jinja` selection. Reported engines distinguish `llm`, `jinja_opt_in_fallback`, `jinja_explicit` and `demo_fixture`. Demo fixtures are never a live recovery strategy.

## State, recoverability and jobs

The run state is separate from the artifact manifest. SQLite stores run summaries, progress events and key-pool reservations/cooldowns. Database transactions explicitly close their connections after commit or rollback. Every generated artifact is hashed in the manifest. A workspace lock serializes generation and review mutations. The dashboard job task stays in the running server process, not a durable external queue; shutdown cancels active tasks and restart labels unfinished runs interrupted.

Completed stages are retained. Retry Phase 3 reads the saved Markdown and does not repeat research/drafting. Editing Markdown marks earlier exports stale, removes them from the current manifest inventory, and invalidates approval. The older physical files can remain on disk but the API will not present them as current exports. Successful regeneration updates hashes and requires review again. `draft.json` remains the original structured draft after manual Markdown edits; the edited Markdown is authoritative for design.

## Routing and cloud privacy

Ollama-only means every model task local. Groq/Gemini/OpenAI/Claude-only means every model task uses that provider. Hybrid routes focus/small/diagnostic tasks to Ollama and drafting/design to Groq. Other models are never silently substituted. Raw private context and a local private-derived plan are withheld from cloud LLMs unless explicitly permitted. Public research and final drafts still go to the selected cloud model.

## Key pooling

Pools can hold more than five keys. Least-used healthy keys receive work. Reservations and cooldowns are transactional and persistent. Keys in the same organization/project share one quota group, including 429 cooldowns and configured rolling request/token budgets. Local budgets are conservative and do not know other applications' usage. A model response is not split across keys; an overlarge single request can fail even with many keys. Unresolved quota failure preserves completed stages instead of fabricating completion.

## Original specification alignment

The three saved-file handoffs, five research artifacts, nine sections, dedicated LLM design prompt, output conventions and manual publishing handoff follow the supplied specification. Intentional adaptations: Python instead of Claude Desktop, Tavily instead of Brave, explicit optional services and fallbacks, provider-neutral structured schemas, local per-run directories, safety checks and editorial approval. The three original prompts are implemented as controlled tasks with validated schemas, not copied verbatim into an autonomous desktop session.
