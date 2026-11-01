import json
import pytest
from newsletter_mcp.config import PoolKeyConfig, QuotaConfig
from newsletter_mcp.key_pool import KeyPool, PoolError, credentials


def setkeys(settings, monkeypatch, provider='groq', count=5, groups=None):
    entries=[]
    for i in range(count):
        var=f'TEST_POOL_{i}'
        monkeypatch.setenv(var,f'synthetic-secret-{provider}-{i}')
        entries.append(PoolKeyConfig(key_env=var,label=f'key-{i}',quota_group=(groups or ['default']*count)[i]))
    settings.llm.key_pools[provider]=entries
    settings.llm.max_rate_wait_seconds=0
    return KeyPool(settings)

async def test_any_number_of_keys_balances_usage_without_leaking_secrets(settings, monkeypatch):
    pool=setkeys(settings,monkeypatch,count=12)
    labels=[]
    for i in range(24):
        lease=await pool.acquire('groq','model',100,60)
        labels.append(lease.credential.label)
        assert 'synthetic-secret' not in repr(lease)
        pool.finish(lease,actual_tokens=80)
    assert all(labels.count(f'key-{i}')==2 for i in range(12))
    assert 'synthetic-secret' not in json.dumps(pool.snapshot())
    assert b'synthetic-secret' not in pool.path.read_bytes()

async def test_shared_cooldown_blocks_all_keys_in_group_and_survives_restart(settings, monkeypatch):
    pool=setkeys(settings,monkeypatch)
    lease=await pool.acquire('groq','model',50,60)
    pool.cool_group(lease,100)
    pool.finish(lease,status='rate_limited')
    for instance in [pool,KeyPool(settings)]:
        with pytest.raises(PoolError,match='cooling down'):
            await instance.acquire('groq','model',50,60)

async def test_independent_authorized_group_is_not_cooled(settings, monkeypatch):
    pool=setkeys(settings,monkeypatch,count=2,groups=['org-a','org-b'])
    first=await pool.acquire('groq','model',50,60)
    pool.cool_group(first,100);pool.finish(first,status='rate_limited')
    second=await pool.acquire('groq','model',50,60)
    assert second.credential.group != first.credential.group
    pool.finish(second)

async def test_concurrency_and_rolling_request_budget_are_group_level(settings, monkeypatch):
    pool=setkeys(settings,monkeypatch,count=5)
    settings.llm.quotas['groq/default']=QuotaConfig(rpm=1,max_concurrency=1)
    lease=await pool.acquire('groq','model',50,60)
    with pytest.raises(PoolError,match='cooling down'): await pool.acquire('groq','model',50,60)
    pool.finish(lease)
    with pytest.raises(PoolError,match='cooling down'): await pool.acquire('groq','another-model',50,60)

async def test_token_reservation_and_completion_reconcile_once(settings, monkeypatch):
    pool=setkeys(settings,monkeypatch,count=1)
    settings.llm.quotas['groq/default']=QuotaConfig(tpm=200)
    lease=await pool.acquire('groq','model',150,60);pool.finish(lease,actual_tokens=40)
    pool.finish(lease,actual_tokens=0)
    second=await pool.acquire('groq','model',150,60);pool.finish(second,actual_tokens=150)
    with pytest.raises(PoolError): await pool.acquire('groq','model',20,60)
    with pool.db() as db: assert db.execute('SELECT tokens FROM pool_keys').fetchone()[0]==190

async def test_single_call_larger_than_quota_cannot_be_split_across_keys(settings, monkeypatch):
    pool=setkeys(settings,monkeypatch)
    settings.llm.quotas['groq/default']=QuotaConfig(tpm=100)
    with pytest.raises(PoolError,match='rotating keys cannot fix'):
        await pool.acquire('groq','model',101,60)

async def test_authorization_failure_disables_only_bad_key(settings, monkeypatch):
    pool=setkeys(settings,monkeypatch,count=2)
    first=await pool.acquire('groq','model',1,60);pool.disable_key(first,'HTTP 401');pool.finish(first,status='failed')
    second=await pool.acquire('groq','model',1,60)
    assert first.credential.fingerprint!=second.credential.fingerprint
    pool.disable_key(second,'HTTP 403');pool.finish(second,status='failed')
    with pytest.raises(PoolError,match='authorization'): await pool.acquire('groq','model',1,60)
    pool.reset_authorization_failures('groq')
    third=await pool.acquire('groq','model',1,60);pool.finish(third)

@pytest.mark.parametrize('provider,prefix',[('groq','GROQ'),('gemini','GEMINI'),('claude','ANTHROPIC'),('openai','OPENAI')])
def test_env_lists_and_duplicates(settings,monkeypatch,provider,prefix):
    monkeypatch.setenv(prefix+'_API_KEYS','["one","two","one"]')
    monkeypatch.setenv(prefix+'_API_KEY','two')
    result=credentials(settings,provider)
    assert len(result)==2 and all(k.group=='default' for k in result)
    settings.llm.key_pools[provider]=[]
    assert credentials(settings,provider)==[]

def test_invalid_json_key_list_gives_config_error(settings,monkeypatch):
    monkeypatch.setenv('GROQ_API_KEYS','[not json')
    with pytest.raises(PoolError): credentials(settings,'groq')

async def test_expired_lease_does_not_permanently_lock_pool(settings,monkeypatch):
    pool=setkeys(settings,monkeypatch,count=1)
    first=await pool.acquire('groq','model',1,-1)
    next_lease=await pool.acquire('groq','model',1,60)
    pool.finish(next_lease)
    with pool.db() as db: assert db.execute('SELECT status FROM pool_requests WHERE id=?',(first.id,)).fetchone()[0]=='interrupted'
