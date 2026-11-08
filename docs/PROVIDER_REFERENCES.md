# Official provider references

Checked while implementing v2 (November 9, 2026). Provider access, model catalogs, pricing and terms can change. Code defaults are starter settings, not guaranteed entitlements. No live cloud inference was performed during the build.

- MCP specification: https://modelcontextprotocol.io/specification/
- Ollama structured outputs: https://docs.ollama.com/capabilities/structured-outputs
- Ollama chat endpoint: https://docs.ollama.com/api/chat
- Groq API reference: https://console.groq.com/docs/api-reference
- Groq model catalog: https://console.groq.com/docs/models
- Groq organization-level quotas and Retry-After: https://console.groq.com/docs/rate-limits
- Gemini generateContent: https://ai.google.dev/api/generate-content
- Gemini project-level quotas: https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini model lifecycle: https://ai.google.dev/gemini-api/docs/deprecations
- OpenAI Chat Completions: https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- OpenAI model catalog: https://developers.openai.com/api/docs/models
- Anthropic Messages API: https://platform.claude.com/docs/en/api/messages/create
- Claude model lifecycle: https://platform.claude.com/docs/en/about-claude/model-deprecations
- Tavily search request schema: https://docs.tavily.com/documentation/api-reference/endpoint/search
- Tavily MCP: https://docs.tavily.com/documentation/mcp
- arXiv API manual: https://info.arxiv.org/help/api/user-manual.html
- GitHub REST search: https://docs.github.com/en/rest/search/search
- Product Hunt API: https://api.producthunt.com/v2/docs
- Product Hunt API use terms: https://api.producthunt.com/v2/docs#terms
- X developer documentation: https://docs.x.com/x-api
- Google installed-app OAuth: https://developers.google.com/identity/protocols/oauth2/native-app
- Google Drive API: https://developers.google.com/workspace/drive/api/guides/about-sdk
- Gmail API: https://developers.google.com/workspace/gmail/api/guides
- Google Calendar API: https://developers.google.com/workspace/calendar/api/guides/overview

Important implementation choices: model-provider contracts are tested with controlled HTTP responses; real account/model access must be checked locally. Groq quotas apply to the organization, Gemini to the project, not individual keys. Default grouping is conservative. Public Tavily excerpts are not a replacement for authenticated Product Hunt/X API semantics.
