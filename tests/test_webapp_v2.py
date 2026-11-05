import asyncio
import io
import json
import os
import time
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient
from newsletter_mcp.config import load_settings
from newsletter_mcp.webapp import create_app, ACTIVE
from newsletter_mcp.state import StateStore

HEADERS={'X-Requested-With':'newsletter-studio'}

def poll(client,identifier,timeout=90):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        row=client.get('/api/runs/'+identifier).json()
        if row['status'] not in ACTIVE:return row
        time.sleep(0.05)
    raise AssertionError('Job did not finish: '+str(row))

def test_settings_persist_validation_and_feature_gate(settings):
    settings.config_path.write_text(yaml.safe_dump(settings.model_dump(mode='json')))
    with TestClient(create_app(settings)) as client:
        assert client.put('/api/settings',json={'phase3':{'mode':'jinja'}},headers=HEADERS).status_code==422
        assert client.put('/api/settings',json={'newsletter':{'timezone':'not/a_timezone'}},headers=HEADERS).status_code==422
        assert client.put('/api/settings',json={'newsletter':{'lookback_days':99}},headers=HEADERS).status_code==422
        assert client.put('/api/settings',json={'mcps':{'filesystem':{'enabled':False}}},headers=HEADERS).status_code==422
        changed=client.put('/api/settings',json={'newsletter':{'name':'Custom Edition'},'llm':{'provider':'hybrid'},'features':{'allow_jinja_fallback':True}},headers=HEADERS)
        assert changed.status_code==200
        assert client.get('/api/settings').json()['llm']['provider']=='hybrid'
        assert client.put('/api/settings',json={'phase3':{'mode':'jinja'}},headers=HEADERS).status_code==200
    loaded=load_settings(settings.config_path)
    assert loaded.newsletter.name=='Custom Edition' and loaded.phase3.mode=='jinja'
    assert 'Custom Edition' in settings.config_path.with_name('config.local.yaml').read_text()


def test_runtime_ollama_endpoint_overrides_stale_dashboard_snapshot(settings, monkeypatch):
    settings.config_path.write_text(yaml.safe_dump(settings.model_dump(mode='json')))
    local = settings.model_dump(mode='json')
    local['llm']['ollama_base_url'] = 'http://ollama:11434'
    settings.config_path.with_name('config.local.yaml').write_text(yaml.safe_dump(local))
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
    loaded = load_settings(settings.config_path)
    assert loaded.llm.ollama_base_url == 'http://127.0.0.1:11434'

@pytest.mark.parametrize('provider',['groq','gemini','openai','claude'])
def test_many_keys_persist_without_response_or_quota_database_disclosure(settings,monkeypatch,provider):
    for i in range(1,9):monkeypatch.delenv(f'{provider.upper()}_POOL_KEY_{i:04d}',raising=False)
    with TestClient(create_app(settings)) as client:
        keys=[{'value':f'SECRET-CANARY-{provider}-{i}','quota_group':'default'} for i in range(8)]
        result=client.post('/api/key-pools',json={'provider':provider,'keys':keys+keys[:1]},headers=HEADERS)
        assert result.status_code==200 and result.json()['configured_keys']==8
        env=(settings.project_root/'.env').read_text()
        assert f'SECRET-CANARY-{provider}-7' in env
        if os.name!='nt':assert (settings.project_root/'.env').stat().st_mode&0o777==0o600
        for endpoint in ['/api/config','/api/settings','/api/quotas','/api/runs']:
            assert 'SECRET-CANARY' not in client.get(endpoint).text
        bad=client.post('/api/key-pools',json={'provider':provider,'keys':[{'value':'SECRET-CANARY','quota_group':'invalid space'}]},headers=HEADERS)
        assert bad.status_code==422 and 'SECRET-CANARY' not in bad.text
        result=client.post('/api/key-pools',json={'provider':provider,'keys':[]},headers=HEADERS)
        assert result.json()['configured_keys']==0
        assert 'SECRET-CANARY' not in (settings.project_root/'.env').read_text()

def test_credential_save_clear_and_oauth_status(settings,monkeypatch):
    monkeypatch.delenv('TAVILY_API_KEY',raising=False)
    with TestClient(create_app(settings)) as client:
        result=client.post('/api/credentials',json={'name':'TAVILY_API_KEY','value':'PRIVATE-TAVILY-KEY'},headers=HEADERS)
        assert result.status_code==200 and 'PRIVATE-TAVILY-KEY' not in result.text
        assert client.get('/api/config').json()['credential_presence']['TAVILY_API_KEY']
        client.post('/api/credentials',json={'name':'TAVILY_API_KEY','value':''},headers=HEADERS)
        assert not client.get('/api/config').json()['credential_presence']['TAVILY_API_KEY']
        config=client.get('/api/config').json()
        assert config['oauth']=={'producthunt':False,'google':False}
        assert client.post('/api/consents',json={},headers=HEADERS).status_code==404


def test_x_uses_only_the_official_app_bearer_credential(settings, monkeypatch):
    monkeypatch.delenv('X_BEARER_TOKEN', raising=False)
    with TestClient(create_app(settings)) as client:
        saved = client.post('/api/credentials', json={
            'name': 'X_BEARER_TOKEN', 'value': 'PRIVATE-X-BEARER'}, headers=HEADERS)
        assert saved.status_code == 200 and 'PRIVATE-X-BEARER' not in saved.text
        assert client.get('/api/config').json()['credential_presence']['X_BEARER_TOKEN']
        legacy = client.post('/api/credentials', json={
            'name': 'X_MCP_ACCESS_TOKEN', 'value': 'legacy'}, headers=HEADERS)
        assert legacy.status_code == 422 and 'legacy' not in legacy.text

def test_actual_async_demo_edit_repackage_and_download_workflow(settings):
    with TestClient(create_app(settings)) as client:
        response=client.post('/api/runs',json={'demo':True,'provider':'hybrid'},headers=HEADERS)
        assert response.status_code==202
        identifier=response.json()['run_id'];prefix='/api/runs/'+identifier
        assert client.put('/api/settings',json={'llm':{'provider':'gemini'}},headers=HEADERS).status_code==409
        row=poll(client,identifier)
        assert row['status']=='awaiting_review',row
        assert len(row['events'])>=6
        html=client.get(prefix+'/files/newsletter.html')
        assert html.status_code==200 and 'max-width:600px' in html.text
        assert html.headers['content-type'].startswith('text/plain')
        bundle=client.get(prefix+'/download')
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
            assert {'newsletter.html','newsletter.txt','newsletter.eml','phase3_report.json'}.issubset(z.namelist())
            assert '.env' not in z.namelist()
        assert client.post(prefix+'/approve',json={'reviewer':'Test Reviewer'},headers=HEADERS).status_code==400
        draft=client.get(prefix+'/files/final_draft_newsletter.md').text
        edited=draft.replace('DEMO EDITION -','EDITED DEMO EDITION -',1)
        result=client.put(prefix+'/draft',json={'markdown':edited},headers=HEADERS)
        assert result.status_code==200,result.text
        assert client.get(prefix).json()['status']=='needs_packaging'
        assert client.get(prefix+'/files/newsletter.html').status_code==409
        bundle=client.get(prefix+'/download')
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:assert 'newsletter.html' not in z.namelist()
        response=client.post(prefix+'/repackage',json={},headers=HEADERS)
        assert response.status_code==202
        assert poll(client,identifier)['status']=='awaiting_review'
        assert 'EDITED DEMO EDITION' in client.get(prefix+'/files/newsletter.html').text
        assert not client.get(prefix+'/files/approval.json').json()['valid']
        assert client.get(prefix+'/files/.env').status_code==404
        assert client.post(prefix+'/cancel',headers=HEADERS).status_code==409

def test_cancel_and_settings_lock_use_actual_job_state(settings,monkeypatch):
    async def waiting(cfg,**kwargs):
        StateStore(cfg.workspace).update(kwargs['identifier'],'research')
        await asyncio.sleep(120)
    monkeypatch.setattr('newsletter_mcp.webapp.run_pipeline',waiting)
    with TestClient(create_app(settings)) as client:
        response=client.post('/api/runs',json={'demo':True},headers=HEADERS)
        identifier=response.json()['run_id']
        assert client.post('/api/runs',json={'demo':True},headers=HEADERS).status_code==409
        assert client.post('/api/runs/'+identifier+'/cancel',headers=HEADERS).status_code==200
        assert client.get('/api/runs/'+identifier).json()['status']=='cancelled'
        assert not client.get('/api/health').json()['busy']

def test_restart_marks_unfinished_jobs_interrupted(settings):
    store=StateStore(settings.workspace)
    identifier='20261109T010203-12345678';store.start(identifier,'groq',False);store.update(identifier,'designing',has_draft=True)
    with TestClient(create_app(settings)) as client:
        row=client.get('/api/runs/'+identifier).json()
        assert row['status']=='interrupted' and row['details']['has_draft']

def test_model_connection_error_is_reported_not_substituted(settings):
    with TestClient(create_app(settings)) as client:
        response=client.post('/api/model-check',json={'provider':'gemini'},headers=HEADERS)
        assert response.status_code==400
        assert 'No gemini API keys' in response.json()['detail']
        assert not client.get('/api/health').json()['busy']

def test_missing_run_and_unknown_mode(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/runs/20261109T010203-12345678').status_code==404
        assert client.post('/api/runs',json={'demo':True,'provider':'unknown'},headers=HEADERS).status_code==422
        assert client.post('/api/runs/20261109T010203-12345678/repackage',json={},headers=HEADERS).status_code==404
