import asyncio
import json
from unittest.mock import AsyncMock
import httpx
import pytest
from pydantic import BaseModel
from newsletter_mcp.llm import LLMClient, LLMError, retry_after_seconds
from newsletter_mcp.models import FocusPlan
from newsletter_mcp.demo import focus

class Tiny(BaseModel):
    status: str

@pytest.mark.parametrize('mode,expected', [
    ('ollama',['ollama']*3),('groq',['groq']*3),('hybrid',['ollama','groq','groq']),
    ('gemini',['gemini']*3),('openai',['openai']*3),('claude',['claude']*3)])
def test_routing_is_explicit_for_each_stage(settings,mode,expected):
    settings.llm.provider=mode
    c=LLMClient(settings)
    assert [c.provider_for(k) for k in ('focus','draft','email')]==expected
    assert c.output_budget('email')==settings.phase3.max_output_tokens
    assert c.output_budget('focus')==settings.llm.small_task_max_tokens

@pytest.mark.parametrize('provider,prefix',[('groq','GROQ'),('gemini','GEMINI'),('openai','OPENAI'),('claude','ANTHROPIC')])
async def test_cloud_payload_response_usage_and_private_context(settings,monkeypatch,provider,prefix):
    monkeypatch.setenv(prefix+'_API_KEY','synthetic-provider-secret')
    settings.llm.provider=provider
    seen=[]
    async def handler(request):
        data=json.loads(request.content);seen.append((request,data))
        if provider in ('groq','openai'):
            assert data['response_format']=={'type':'json_object'}
            assert data['max_completion_tokens']==settings.phase3.max_output_tokens
            assert request.headers['Authorization']=='Bearer synthetic-provider-secret'
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"status":"ok"}'},'finish_reason':'stop'}], 'usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':14}})
        if provider=='gemini':
            assert data['generationConfig']['responseMimeType']=='application/json'
            assert request.headers['x-goog-api-key']=='synthetic-provider-secret'
            return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'{"status":"ok"}'}]}}], 'usageMetadata':{'totalTokenCount':14}})
        assert data['tool_choice']=={'type':'tool','name':'emit_structured_result'}
        assert request.headers['x-api-key']=='synthetic-provider-secret'
        return httpx.Response(200,json={'stop_reason':'tool_use','content':[{'type':'tool_use','name':'emit_structured_result','input':{'status':'ok'}}], 'usage':{'input_tokens':10,'output_tokens':4}})
    real=httpx.AsyncClient
    monkeypatch.setattr('newsletter_mcp.llm.httpx.AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler)))
    c=LLMClient(settings)
    result=await c.generate(Tiny,'Format output',{}, {'secret':'PRIVATE-CANARY'},task_kind='email')
    assert result.status=='ok'
    assert 'PRIVATE-CANARY' not in str(seen[0][1])
    assert c.audit[0]['timeout_seconds']==settings.llm.heavy_timeout_seconds
    assert 'synthetic-provider-secret' not in str(c.audit)
    assert c.pool.snapshot()['providers'][provider][0]['tokens']==14

async def test_429_cools_shared_group_and_does_not_rotate_around_limit(settings,monkeypatch):
    monkeypatch.setenv('GROQ_API_KEYS','one,two,three,four,five')
    settings.llm.provider='groq';settings.llm.max_rate_wait_seconds=0
    calls=[]
    async def handler(request):
        calls.append(request)
        return httpx.Response(429,headers={'retry-after':'120'},json={'error':{'message':'Rate limited'}})
    real=httpx.AsyncClient
    monkeypatch.setattr('newsletter_mcp.llm.httpx.AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('newsletter_mcp.llm.asyncio.sleep',AsyncMock())
    c=LLMClient(settings)
    with pytest.raises(LLMError,match='cooling down'):
        await c.generate(Tiny,'Test',{})
    assert len(calls)==1
    assert all(k['state']=='cooling_down' for k in c.pool.snapshot()['providers']['groq'])

async def test_invalid_key_tries_next_credential_without_quota_reset(settings,monkeypatch):
    monkeypatch.setenv('GROQ_API_KEYS','invalid-key,valid-key')
    settings.llm.provider='groq'
    calls=[]
    async def handler(request):
        calls.append(request.headers['Authorization'])
        if len(calls)==1:return httpx.Response(401,json={'error':'Unauthorized'})
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"status":"ok"}'},'finish_reason':'stop'}]})
    real=httpx.AsyncClient
    monkeypatch.setattr('newsletter_mcp.llm.httpx.AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('newsletter_mcp.llm.asyncio.sleep',AsyncMock())
    c=LLMClient(settings)
    assert (await c.generate(Tiny,'Test',{})).status=='ok'
    assert calls==['Bearer invalid-key','Bearer valid-key']

@pytest.mark.parametrize('provider,reply', [
    ('ollama',{'done_reason':'length'}),('groq',{'choices':[{'finish_reason':'length'}]}),
    ('openai',{'choices':[{'finish_reason':'content_filter'}]}),
    ('gemini',{'candidates':[{'finishReason':'MAX_TOKENS'}]}),('claude',{'stop_reason':'max_tokens'})])
def test_truncated_outputs_never_accepted(provider,reply):
    with pytest.raises(LLMError):LLMClient._content(provider,reply)

def test_retry_after_parsing():
    assert retry_after_seconds(httpx.Response(429,headers={'retry-after':'30'}))==30
    assert retry_after_seconds(httpx.Response(429,json={'error':{'details':[{'@type':'type.googleapis.com/google.rpc.RetryInfo','retryDelay':'4.5s'}]}}))==4.5
    assert retry_after_seconds(httpx.Response(429,text='n/a'))==60

async def test_transient_error_retries_preserve_provider(settings,monkeypatch):
    settings.llm.request_retries=2
    calls=[]
    async def handler(request):
        calls.append(request.url.host)
        if len(calls)<3:return httpx.Response(503)
        return httpx.Response(200,json={'message':{'content':'{"status":"ok"}'},'done_reason':'stop'})
    real=httpx.AsyncClient
    monkeypatch.setattr('newsletter_mcp.llm.httpx.AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr('newsletter_mcp.llm.asyncio.sleep',AsyncMock())
    c=LLMClient(settings)
    assert (await c.generate(Tiny,'Test',{})).status=='ok'
    assert calls==['127.0.0.1']*3

async def test_schema_failure_has_bounded_repair_attempts(settings,monkeypatch):
    settings.llm.validation_attempts=3
    c=LLMClient(settings)
    request=AsyncMock(return_value='{"wrong":true}')
    monkeypatch.setattr(c,'_request',request)
    with pytest.raises(LLMError,match='3 attempts'):await c.generate(Tiny,'Test',{})
    assert request.call_count==3

async def test_timeout_is_bounded_and_not_replaced_by_mock(settings,monkeypatch):
    c=LLMClient(settings)
    settings.llm.task_deadline_seconds=0.01
    async def slow(*args,**kwargs):await asyncio.sleep(0.2)
    monkeypatch.setattr(c,'_request',slow)
    with pytest.raises(LLMError,match='deadline'):await c.generate(Tiny,'Test',{})

def test_phase3_markdown_never_truncated(settings):
    c=LLMClient(settings)
    with pytest.raises(LLMError):
        c.messages(Tiny,'Build',{'markdown':'x'*200000},None,'groq')
