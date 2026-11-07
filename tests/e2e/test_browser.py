import json
import os
from pathlib import Path
import time
import zipfile

import httpx
from playwright.sync_api import expect

def no_error(page):
    assert not page.locator("#global-error").is_visible(), page.locator("#global-error-text").text_content()


def nav(page,destination):
    page.evaluate('(value)=>location.hash="/"+value',destination)
    page.locator('#page-'+destination).wait_for(state='visible')


def test_all_pages_settings_and_key_pools_use_real_backend(ui,live_server):
    page=ui
    no_error(page)
    for name in ('editions','integrations','settings','guide','dashboard'):
        nav(page,name);no_error(page)
    nav(page,'settings')
    page.locator('[data-setting="newsletter.name"]').fill('Browser-tested Dispatch')
    page.locator('[data-setting="llm.provider"]').select_option('hybrid')
    page.locator('#jinja-flag').check()
    expect(page.locator('#phase3-mode option[value="jinja"]')).to_be_enabled()
    page.locator('#phase3-mode').select_option('jinja')
    page.locator('#jinja-flag').uncheck()
    expect(page.locator('#phase3-mode')).to_have_value('llm')
    page.locator('#save-settings').click()
    expect(page.locator('#settings-save-status')).to_contain_text('saved',timeout=30000)
    no_error(page)
    backend=httpx.get(live_server['base']+'/api/settings',headers={'Authorization':'Bearer '+live_server['token']}).json()
    assert backend['newsletter']['name']=='Browser-tested Dispatch'
    assert backend['llm']['provider']=='hybrid' and not backend['features']['allow_jinja_fallback']
    page.locator('#pool-provider').select_option('gemini')
    page.locator('#pool-values').fill('\n'.join('SYNTHETIC-KEY-'+str(i) for i in range(7)))
    page.locator('#save-pool').click()
    expect(page.locator('#pool-summary')).to_contain_text('7 configured keys',timeout=30000)
    expect(page.locator('#pool-values')).to_have_value('')
    assert 'SYNTHETIC-KEY' not in page.locator('#pool-summary').inner_text()
    page.locator('#clear-pool').click()
    expect(page.locator('#pool-summary')).to_contain_text('0 configured keys')
    # Invalid quota JSON is handled in the UI, not silently discarded.
    page.locator('#quota-json').fill('not JSON')
    page.locator('#save-settings').click()
    page.locator('#global-error').wait_for(state='visible')
    page.locator('#dismiss-error').click()
    page.locator('#quota-json').fill('{}');page.locator('#save-settings').click()
    expect(page.locator('#settings-save-status')).to_contain_text('saved')
    no_error(page)


def test_integrations_fallback_preferences_and_doctor(ui,live_server):
    page=ui;nav(page,'integrations')
    expect(page.locator('.integration-card')).to_have_count(9)
    expect(page.locator('#toggle-filesystem')).to_be_disabled()
    page.locator('#toggle-producthunt').uncheck()
    expect(page.locator('#toggle-producthunt')).not_to_be_checked()
    page.locator('#fallback-producthunt').uncheck();page.locator('#save-fallbacks').click()
    page.wait_for_timeout(350)
    page.locator('#fallback-producthunt').check();page.locator('#save-fallbacks').click()
    page.locator('#doctor-button').click()
    expect(page.locator('#doctor-button')).to_be_enabled(timeout=120000)
    expect(page.locator('#doctor-scope')).to_contain_text('not',ignore_case=True)
    no_error(page)


def test_generate_review_edit_repackage_preview_and_download(ui,live_server,tmp_path):
    page=ui;nav(page,'dashboard')
    page.locator('#run-mode').select_option('hybrid')
    page.locator('#run-type').select_option('demo')
    page.locator('#generate-button').click()
    page.locator('#page-review').wait_for(state='visible')
    expect(page.locator('#review-badges')).to_contain_text('Awaiting review',timeout=120000)
    no_error(page)
    expect(page.locator('#approve-run')).to_be_disabled()
    expect(page.frame_locator('#email-preview').locator('h2')).to_have_count(9,timeout=30000)
    page.locator('[data-device="mobile"]').click()
    expect(page.locator('#email-preview')).to_have_class('mobile')
    page.locator('[data-device="desktop"]').click()
    for tab in ('draft','research','checks','activity','preview'):
        page.locator(f'[data-tab="{tab}"]').click()
        page.locator('#tab-'+tab).wait_for(state='visible')
        page.wait_for_timeout(250)
        no_error(page)
    page.locator('[data-tab="research"]').click()
    for channel in ('web','arxiv','github','producthunt','twitter'):
        page.locator('#research-channel').select_option(channel)
        expect(page.locator('#research-raw')).not_to_be_empty(timeout=30000)
    page.locator('[data-tab="draft"]').click()
    expect(page.locator('#draft-editor')).to_contain_text('',timeout=30000)
    page.wait_for_function("document.querySelector('#draft-editor').value.startsWith('#')")
    original=page.locator('#draft-editor').input_value()
    page.locator('#draft-editor').fill(original.replace('DEMO EDITION -','EDITED DEMO EDITION -',1))
    page.locator('#save-draft').click()
    expect(page.locator('#review-badges')).to_contain_text('Needs Phase 3',timeout=30000)
    expect(page.locator('#save-draft')).to_be_disabled()
    expect(page.locator('[data-download="newsletter.html"]')).to_be_disabled()
    page.locator('#retry-phase3').click()
    expect(page.locator('#review-badges')).to_contain_text('Awaiting review',timeout=120000)
    expect(page.frame_locator('#email-preview').locator('h1')).to_contain_text('EDITED DEMO EDITION',timeout=30000)
    no_error(page)
    # Blob download bytes come from the real backend, including through the optional test transport bridge.
    with page.expect_download(timeout=60000) as event:
        page.locator('[data-download="bundle"]').click()
    download=event.value;target=tmp_path/'edition.zip';download.save_as(target)
    with zipfile.ZipFile(target) as z:
        assert 'newsletter.eml' in z.namelist()
        assert 'EDITED DEMO EDITION' in z.read('newsletter.html').decode()
    for artifact in ('newsletter.html','newsletter.txt','newsletter.eml'):
        with page.expect_download(timeout=30000) as event:page.locator(f'[data-download="{artifact}"]').click()
        assert event.value.suggested_filename==artifact
    no_error(page)
    shots=Path(os.getenv('UI_SCREENSHOTS',str(tmp_path)))
    shots.mkdir(parents=True,exist_ok=True)
    page.evaluate('document.querySelectorAll(".toast").forEach(node=>node.remove())')
    page.screenshot(path=str(shots/'review-desktop.png'),full_page=True,animations='disabled')
    nav(page,'dashboard');page.wait_for_timeout(350)
    page.screenshot(path=str(shots/'dashboard-desktop.png'),full_page=True,animations='disabled')


def test_live_confirmation_cancel_and_missing_model_feedback(ui,live_server):
    page=ui;nav(page,'dashboard')
    page.locator('#run-type').select_option('live')
    page.locator('#generate-button').click()
    expect(page.locator('#live-dialog')).to_be_visible()
    page.locator('#live-dismiss').click()
    expect(page.locator('#live-dialog')).not_to_be_visible()
    page.locator('#run-type').select_option('demo')
    page.locator('#generate-button').click()
    page.locator('#cancel-run').wait_for(state='visible')
    page.locator('#cancel-run').click()
    expect(page.locator('#review-badges')).to_contain_text('Cancelled',timeout=90000)
    no_error(page)
    nav(page,'settings')
    page.locator('[data-setting="llm.provider"]').select_option('gemini')
    page.locator('#save-settings').click()
    expect(page.locator('#settings-save-status')).to_contain_text('saved')
    page.locator('#model-check').click()
    page.locator('#global-error').wait_for(state='visible',timeout=30000)
    expect(page.locator('#global-error-text')).to_contain_text('No gemini API keys')
    page.locator('#dismiss-error').click()
    expect(page.locator('#model-check')).to_be_enabled()
    no_error(page)



def test_credentials_permissions_and_all_mode_preferences(ui,live_server):
    page=ui;nav(page,'settings')
    headers={'Authorization':'Bearer '+live_server['token']}
    for mode in ('ollama','groq','hybrid','gemini','openai','claude'):
        page.locator('[data-setting="llm.provider"]').select_option(mode)
        page.locator('#save-settings').click()
        expect(page.locator('#save-settings')).to_be_enabled(timeout=30000)
        assert httpx.get(live_server['base']+'/api/settings',headers=headers).json()['llm']['provider']==mode
    page.locator('#credential-name').select_option('TAVILY_API_KEY')
    page.locator('#credential-value').fill('SYNTHETIC-BROWSER-KEY')
    page.locator('#credential-form button[type="submit"]').click()
    expect(page.locator('#credential-value')).to_have_value('')
    assert httpx.get(live_server['base']+'/api/config',headers=headers).json()['credential_presence']['TAVILY_API_KEY']
    page.locator('#clear-credential').click()
    expect(page.locator('#credential-state')).to_contain_text('No credential is configured')
    page.locator('#paid-consent').check();page.locator('#producthunt-consent').check()
    page.locator('#consent-form button[type="submit"]').click()
    page.wait_for_timeout(400)
    response=httpx.get(live_server['base']+'/api/config',headers=headers).json()
    assert response['paid_services_allowed'] and response['producthunt_use_authorized']
    page.locator('#paid-consent').uncheck();page.locator('#producthunt-consent').uncheck()
    page.locator('#consent-form button[type="submit"]').click()
    no_error(page)

def test_mobile_navigation_and_layout(ui,tmp_path):
    page=ui;page.set_viewport_size({'width':390,'height':844})
    nav(page,'dashboard')
    page.locator('#menu-toggle').click()
    expect(page.locator('#menu-toggle')).to_have_attribute('aria-expanded','true')
    page.locator('[data-nav="settings"]').click()
    expect(page.locator('#page-settings')).to_be_visible()
    expect(page.locator('#menu-toggle')).to_have_attribute('aria-expanded','false')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    nav(page,'dashboard')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    no_error(page)
    shots=Path(os.getenv('UI_SCREENSHOTS',str(tmp_path)));shots.mkdir(parents=True,exist_ok=True)
    page.wait_for_timeout(350)
    page.screenshot(path=str(shots/'dashboard-mobile.png'),full_page=True,animations='disabled')
