# Migrating from v1

Back up your old project and workspace. Extract v2 into a separate folder, create a fresh virtual environment, install its dependencies and run the offline demo first. Do not overlay the entire old config onto the new one: v2 adds flags, providers, quotas, deadlines and research fallbacks.

Transfer only your own credentials into a private `.env` or use Settings & keys. This project expects `PRODUCTHUNT_TOKEN`, not the differently named example variables from the original Claude Desktop specification. There is no `LLM_FALLBACK_PROVIDER` mode anymore; use explicit `hybrid` routing when desired. Claude uses `ANTHROPIC_API_KEY(S)`, Gemini uses `GEMINI_API_KEY(S)`.

You can point v2's workspace at a copy of your earlier workspace. Back up before modifying anything. Newly generated editions follow v2. `newsletter render RUN_ID` / Retry Phase 3 reads the saved Markdown; legacy editions must preserve the required nine section headings and safe links. Legacy preview artifacts may exist but are not evidence that v2 ran its new design stage. Existing review must be repeated after regeneration. Original `draft.json` is retained after manual Markdown edits; Markdown is the authoritative design input.

Default differences: live Phase 3 now calls the selected LLM; Jinja fallback is OFF; Product Hunt/X public Tavily fallback is ON; six modes exist; dashboard jobs are asynchronous in the local server process; settings are saved separately in `config.local.yaml`. Cloud privacy remains opt-in.
