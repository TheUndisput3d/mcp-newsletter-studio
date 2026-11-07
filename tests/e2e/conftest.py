"""Real browser + running backend. Optional binding bridge for network-restricted sandboxes.

Normal: RUN_UI_TESTS=1 pytest tests/e2e
Restricted sandbox only: RUN_UI_TESTS=1 UI_TRANSPORT_BRIDGE=1 ...
The bridge changes browser transport, never application responses or business logic.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest
import yaml
from playwright.sync_api import sync_playwright
from newsletter_mcp.config import Settings

TOKEN='synthetic-ui-session-token'

@pytest.fixture(scope='module')
def live_server(tmp_path_factory):
    if os.getenv('RUN_UI_TESTS')!='1':pytest.skip('Set RUN_UI_TESTS=1 and install Playwright Chromium to run browser integration tests')
    root=tmp_path_factory.mktemp('ui-server')
    cfg=Settings();cfg.newsletter.workspace=str(root/'output');cfg.llm.request_retries=0;cfg.llm.timeout_seconds=10
    (root/'config.yaml').write_text(yaml.safe_dump(cfg.model_dump(mode='json')))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env=os.environ.copy()
    for name in list(env):
        if name.startswith(('GROQ_','GEMINI_','OPENAI_','ANTHROPIC_','CLAUDE_','OLLAMA_','LLM_','TAVILY_','PRODUCTHUNT_','X_BEARER','GOOGLE_','DASHBOARD_')):env.pop(name)
    env['DASHBOARD_TOKEN']=TOKEN
    command='from newsletter_mcp.config import load_settings; from newsletter_mcp.webapp import create_app; import uvicorn,sys; uvicorn.run(create_app(load_settings(sys.argv[1])),host="127.0.0.1",port=int(sys.argv[2]),log_level="warning")'
    log=open(root/'server.log','w')
    proc=subprocess.Popen([sys.executable,'-c',command,str(root/'config.yaml'),str(port)],env=env,stdout=log,stderr=log)
    base=f'http://127.0.0.1:{port}'
    for _ in range(200):
        try:
            if httpx.get(base+'/api/health',headers={'Authorization':'Bearer '+TOKEN},timeout=2).status_code==200:break
        except httpx.HTTPError:pass
        time.sleep(.05)
    else:
        proc.terminate();raise AssertionError((root/'server.log').read_text())
    yield {'base':base,'root':root,'token':TOKEN}
    proc.terminate()
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:proc.kill();proc.wait()
    log.close()

@pytest.fixture(scope='module')
def browser(live_server):
    with sync_playwright() as p:
        options={'headless':True,'args':['--no-sandbox']}
        if os.getenv('CHROMIUM_EXECUTABLE'):options['executable_path']=os.environ['CHROMIUM_EXECUTABLE']
        b=p.chromium.launch(**options)
        yield b
        b.close()

def mount_page(page,server):
    base=server['base']
    if os.getenv('UI_TRANSPORT_BRIDGE')=='1':
        def bridge(path,init):
            if not path.startswith('/api/'):raise AssertionError('Unexpected browser request: '+path)
            headers=init.get('headers',{})|{'Origin':base}
            response=httpx.request(init.get('method','GET'),base+path,headers=headers,content=init.get('body'),timeout=180)
            return {'status':response.status_code,'headers':dict(response.headers),'body':base64.b64encode(response.content).decode()}
        page.expose_function('__realBackendTransport',bridge)
        document=httpx.get(base).text.replace('<link rel="stylesheet" href="/static/studio.css">','').replace('<script src="/static/studio.js" defer></script>','')
        page.set_content(document)
        page.add_style_tag(content=httpx.get(base+'/static/studio.css').text)
        page.evaluate('''() => { window.fetch=async (path,init={}) => {
          const r=await window.__realBackendTransport(path,{method:init.method||"GET",headers:init.headers||{},body:init.body||null});
          const bytes=Uint8Array.from(atob(r.body),c=>c.charCodeAt(0));return new Response(bytes,{status:r.status,headers:r.headers});
        }; }''')
        page.add_script_tag(content=httpx.get(base+'/static/studio.js').text)
    else:page.goto(base,wait_until='networkidle')
    page.locator('#auth-dialog').wait_for(state='visible')
    page.locator('#auth-token').fill(server['token'])
    page.locator('#auth-form button[type="submit"]').click()
    page.locator('#auth-dialog').wait_for(state='hidden')
    page.wait_for_function("document.querySelector('#workspace-path').textContent !== 'Loading...'")
    page.locator('#global-error').wait_for(state='hidden')

@pytest.fixture
def ui(browser,live_server):
    context=browser.new_context(viewport={'width':1440,'height':1050},accept_downloads=True)
    page=context.new_page();page.set_default_timeout(15000)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    mount_page(page,live_server)
    yield page
    assert errors==[]
    context.close()


def no_error(page):
    assert not page.locator('#global-error').is_visible(), page.locator('#global-error-text').text_content()
