import json
from unittest.mock import AsyncMock

import httpx
import pytest

from newsletter_mcp.demo import focus
from newsletter_mcp.llm import LLMClient, LLMError
from newsletter_mcp.models import FocusPlan


def test_private_context_does_not_leak_to_groq(settings):
    client = LLMClient(settings)
    private = {"feedback": "PRIVATE_CANARY"}
    local = client.messages(FocusPlan, "Write", {}, private, "ollama")
    cloud = client.messages(FocusPlan, "Write", {}, private, "groq")
    assert "PRIVATE_CANARY" in str(local)
    assert "PRIVATE_CANARY" not in str(cloud)
    settings.newsletter.allow_cloud_context = True
    assert "PRIVATE_CANARY" in str(client.messages(FocusPlan, "Write", {}, private, "groq"))


async def test_schema_validation_retries_once(settings, monkeypatch):
    client = LLMClient(settings)
    mock = AsyncMock(side_effect=['{"not_the_schema": true}', focus().model_dump_json()])
    monkeypatch.setattr(client, "_request", mock)
    result = await client.generate(FocusPlan, "Plan", {})
    assert len(result.themes) == 3 and mock.call_count == 2


async def test_no_implicit_provider_fallback(settings, monkeypatch):
    client = LLMClient(settings)
    mock = AsyncMock(side_effect=LLMError("Missing Ollama model"))
    monkeypatch.setattr(client, "_request", mock)
    monkeypatch.setattr("newsletter_mcp.llm.asyncio.sleep", AsyncMock())
    with pytest.raises(LLMError):
        await client.generate(FocusPlan, "Plan", {})
    assert all(call.args[0] == "ollama" for call in mock.call_args_list)


async def test_real_provider_http_payloads(settings, monkeypatch):
    real_client = httpx.AsyncClient
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": focus().model_dump_json()}, "done": True})
        return httpx.Response(200, json={"choices": [{"message": {"content": focus().model_dump_json()}, "finish_reason": "stop"}]})
    def factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), timeout=20)
    monkeypatch.setattr("newsletter_mcp.llm.httpx.AsyncClient", factory)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    settings.llm.ollama_model = "llama3.1:latest"
    client = LLMClient(settings)
    messages = client.messages(FocusPlan, "Plan", {}, None, "ollama")
    await client._request("ollama", FocusPlan, messages)
    await client._request("groq", FocusPlan, messages)
    assert isinstance(requests[0][1]["format"], dict)
    assert requests[1][1]["response_format"] == {"type": "json_object"}
    assert requests[1][0].headers["Authorization"] == "Bearer test-key"
    assert client.calls == 2


def test_qwen3_uses_generic_json_to_avoid_ollama_schema_grammar_limit(settings):
    settings.llm.ollama_model = "qwen3:4b-instruct-2507-q4_K_M"
    client = LLMClient(settings)
    _, _, payload = client._payload("ollama", FocusPlan, [], 10, "")
    assert payload["format"] == "json"


def test_ollama_context_is_configurable(settings):
    settings.llm.ollama_num_ctx = 16384
    client = LLMClient(settings)
    _, _, payload = client._payload("ollama", FocusPlan, [], 10, "")
    assert payload["options"]["num_ctx"] == 16384


async def test_llm_call_budget(settings):
    client = LLMClient(settings)
    client.calls = settings.newsletter.max_llm_calls
    with pytest.raises(LLMError, match="budget"):
        await client._request("ollama", FocusPlan, [])


def test_large_context_is_bounded_without_mutating_sources(settings):
    client = LLMClient(settings)
    evidence = [{"id": f"web_{i}", "summary": "x" * 4000} for i in range(30)]
    private = {"documents": [{"text": "secret " * 3000}], "feedback": []}
    messages = client.messages(FocusPlan, "Plan", {"evidence": evidence}, private, "ollama")
    assert len(messages[1]["content"]) <= settings.newsletter.max_input_characters
    assert '"context_truncated": true' in messages[1]["content"]
    assert len(evidence[0]["summary"]) == 4000
