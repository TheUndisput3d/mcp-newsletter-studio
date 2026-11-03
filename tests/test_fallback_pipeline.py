"""Real nine-server MCP transport with synthetic providers and model responses."""
import json
from datetime import datetime,timedelta,timezone
from unittest.mock import AsyncMock

import pytest
from newsletter_mcp import demo
from newsletter_mcp.models import (Source, FocusPlan, EditionCopy, EditorialItemCopy, Item,
                                   LearningTopicSelection, TutorialCopy)
from newsletter_mcp.llm import LLMError
from newsletter_mcp.email_builder import EmailFragment, EmailPackage, demo_package
from newsletter_mcp.pipeline import (run_pipeline, tavily_fallback, primary_web_queries, PipelineError, repackage_run, read_artifact, RESEARCH_FILES)
from newsletter_mcp.registry import MCPHub
from newsletter_mcp.state import StateStore

@pytest.mark.parametrize('channel',['producthunt','twitter'])
async def test_fallback_calls_tavily_mcp_with_correct_query_and_provenance(settings,channel):
    hub=AsyncMock();hub.search_web.return_value={'items':[{'title':'A public page','url':'https://example.com/page','summary':'An excerpt','metadata':{}}]}
    end=datetime.now(timezone.utc)
    result=await tavily_fallback(hub,settings,channel,end-timedelta(days=7),end,3)
    assert hub.search_web.call_args.kwargs['topic']=='general'
    metadata=result['items'][0]['metadata']
    assert metadata['retrieval_method']=='tavily_fallback' and not metadata['platform_authenticated']
    assert metadata['original_channel']==channel
    assert hub.search_web.call_count<=settings.research_fallback.max_searches_per_channel

def test_primary_web_queries_are_diverse_public_and_bounded(settings):
    settings.newsletter.topics = ["open-source AI", "AI developer tools"]
    settings.newsletter.max_search_calls = 3
    queries = primary_web_queries(settings)
    assert len(queries) == 3 and len(set(queries)) == 3
    assert queries[0].startswith("open-source AI") and "releases" in queries[0]
    assert queries[1].startswith("AI developer tools") and "deployment" in queries[1]
    assert "governance" in queries[2]

class FixtureHub(MCPHub):
    failure_style='missing'
    instances=[]
    def __init__(self,settings,**kw):
        super().__init__(settings,demo=True)
        self.read_paths=[];self.fallback_calls=[];self.__class__.instances.append(self)
    async def __aenter__(self):
        await super().__aenter__()
        if self.failure_style=='missing':
            self.status['producthunt']['state']='unconfigured'
            self.status['twitter']['state']='disabled'
        return self
    async def call(self,server,tool,args=None):
        if tool in ('search_products','search_posts'):
            if self.failure_style=='failed':raise RuntimeError('synthetic upstream failure')
            if self.failure_style=='empty':return {'items':[]}
        return await super().call(server,tool,args)
    async def read(self,path):
        self.read_paths.append(path)
        return await super().read(path)
    async def search_web(self,*args,**kwargs):
        if kwargs.get('topic')=='general':
            channel='twitter' if kwargs.get('include_domains') else 'producthunt'
            self.fallback_calls.append(channel)
            return demo.records(channel)
        return await super().search_web(*args,**kwargs)

class FixtureLLM:
    instances=[]; fail_email=False
    def __init__(self,settings):
        self.settings=settings;self.calls=0;self.audit=[];self.inputs=[];self.private_inputs=[];self.__class__.instances.append(self)
    async def generate(self,schema,task,public_context,private_context=None,validator=None,**kwargs):
        self.calls+=1;self.inputs.append((schema,public_context));self.private_inputs.append((schema,private_context))
        if schema is FocusPlan:result=demo.focus()
        elif schema is EditionCopy:
            result=EditionCopy(title='Synthetic edition',preheader='Synthetic preheader',introduction='Synthetic introduction.',closing='Synthetic closing.')
        elif schema is EditorialItemCopy:
            source=public_context['source']
            result=EditorialItemCopy(title=source['title'],body='Synthetic sourced editorial item.')
        elif schema is LearningTopicSelection:
            result=LearningTopicSelection(primary_index=2, lesson_type='concept',
                                          lesson_angle='Explain the reusable technical mechanism in the selected research.')
        elif schema is TutorialCopy:
            source=public_context['evidence'][0]
            result=TutorialCopy(title='Learn from '+source['title'],lesson_type='concept',
                objective='Understand the source-backed technical mechanism and why it matters in practical AI work.',
                explanation=('The selected research describes a technical mechanism that can be understood without turning the lesson into a news recap. '
                             'Focus on the relationship between the stated problem, the mechanism used to address it, and the limits of the available evidence. '
                             'This keeps the lesson practical while avoiding claims that the source does not establish.'),
                steps=[],code='',takeaway='A useful AI lesson explains one supported mechanism and its limits clearly enough to reuse elsewhere.')
        elif schema is EmailPackage:
            if self.fail_email:raise LLMError('synthetic Phase 3 failure')
            result=demo_package(public_context['markdown'])
        elif schema is EmailFragment:
            if self.fail_email:raise LLMError('synthetic Phase 3 failure')
            result=EmailFragment(html=public_context['source_html'])
        else:raise AssertionError(schema)
        if validator:validator(result)
        return result

@pytest.mark.parametrize('failure_style',['missing','failed','empty'])
async def test_full_pipeline_tavily_fallback_and_real_saved_stage_handoffs(settings,monkeypatch,failure_style):
    monkeypatch.setattr(FixtureHub,'failure_style',failure_style)
    monkeypatch.setattr('newsletter_mcp.pipeline.MCPHub',FixtureHub)
    monkeypatch.setattr('newsletter_mcp.pipeline.LLMClient',FixtureLLM)
    manifest=await run_pipeline(settings,demo=False)
    assert manifest['status']=='awaiting_review'
    assert manifest['phase3']['engine']=='llm_fragmented'
    assert manifest['channels']['producthunt']['fallback']
    assert manifest['channels']['twitter']['fallback']
    hub=FixtureHub.instances[-1];llm=FixtureLLM.instances[-1]
    assert set(hub.fallback_calls)=={'producthunt','twitter'}
    assert all(any(p.endswith(name) for p in hub.read_paths) for name in RESEARCH_FILES.values())
    assert hub.read_paths[-1].endswith('final_draft_newsletter.md')
    staged_inputs=[c for schema,c in llm.inputs if schema is EditorialItemCopy]
    assert staged_inputs
    assert all('source' in item for item in staged_inputs)
    assert len(staged_inputs) <= 16
    selection_inputs=[c for schema,c in llm.inputs if schema is LearningTopicSelection]
    assert len(selection_inputs)==1 and len(selection_inputs[0]['candidates'])>1
    tutorial_inputs=[c for schema,c in llm.inputs if schema is TutorialCopy]
    assert len(tutorial_inputs)==1 and tutorial_inputs[0]['evidence'][0]['id']
    assert all(private is None for schema,private in llm.private_inputs
               if schema in {LearningTopicSelection,TutorialCopy})
    markdown=await read_artifact(settings,manifest['run_id'],'final_draft_newsletter.md')
    assert '## AI Community Highlights' in markdown
    assert 'Tavily' in markdown
    assert 'Learn from' in markdown
    fragment_inputs=[c for schema,c in llm.inputs if schema is EmailFragment]
    assert len(fragment_inputs)==10 and all('source_html' in value for value in fragment_inputs)

async def test_phase3_failure_keeps_draft_and_retry_does_not_repeat_research(settings,monkeypatch):
    monkeypatch.setattr('newsletter_mcp.pipeline.MCPHub',FixtureHub)
    monkeypatch.setattr('newsletter_mcp.pipeline.LLMClient',FixtureLLM)
    monkeypatch.setattr(FixtureLLM,'fail_email',True)
    with pytest.raises(PipelineError,match='Jinja fallback is OFF'):await run_pipeline(settings)
    row=StateStore(settings.workspace).list_runs()[0]
    assert row['status']=='failed' and row['details']['has_draft']
    manifest=json.loads(await read_artifact(settings,row['id'],'manifest.json'))
    assert all(f in manifest['files'] for f in RESEARCH_FILES.values())
    assert 'final_draft_newsletter.md' in manifest['files'] and 'newsletter.html' not in manifest['files']
    prior_hubs=len(FixtureHub.instances)
    monkeypatch.setattr(FixtureLLM,'fail_email',False)
    result=await repackage_run(settings,row['id'])
    assert result['status']=='awaiting_review' and len(FixtureHub.instances)==prior_hubs
    assert all(s is EmailFragment for s,c in FixtureLLM.instances[-1].inputs)

async def test_disabled_fallback_produces_notices_not_inventions(settings,monkeypatch):
    monkeypatch.setattr('newsletter_mcp.pipeline.MCPHub',FixtureHub)
    monkeypatch.setattr('newsletter_mcp.pipeline.LLMClient',FixtureLLM)
    settings.research_fallback.producthunt_to_tavily=False
    settings.research_fallback.twitter_to_tavily=False
    result=await run_pipeline(settings)
    assert not FixtureHub.instances[-1].fallback_calls
    assert result['channels']['producthunt']['count']==0
    assert result['channels']['twitter']['count']==0
    assert 'product_hunt_research.md' in result['files'] and 'twitter_research.md' in result['files']

async def test_live_branch_editorial_approval_hashes_and_tamper_rejection(settings,monkeypatch):
    """Credential-free provider fixtures; this tests approval logic, not live API access."""
    from newsletter_mcp.pipeline import approve_run, save_markdown
    monkeypatch.setattr('newsletter_mcp.pipeline.MCPHub',FixtureHub)
    monkeypatch.setattr('newsletter_mcp.pipeline.LLMClient',FixtureLLM)
    result=await run_pipeline(settings,demo=False)
    identifier=result['run_id']
    approval=await approve_run(settings,identifier,'Test-only Reviewer')
    assert approval['artifact_hashes']==result['files']
    assert 'no email was sent' in approval['scope']
    assert StateStore(settings.workspace).get(identifier)['status']=='approved'
    target=settings.workspace/'issues'/identifier/'newsletter.html'
    target.write_text(target.read_text()+'\n<!-- changed outside application -->')
    with pytest.raises(PipelineError,match='changed after packaging'):
        await approve_run(settings,identifier,'Test-only Reviewer')
    markdown=await read_artifact(settings,identifier,'final_draft_newsletter.md')
    await save_markdown(settings,identifier,markdown.replace('DEMO EDITION -','EDITED DEMO EDITION -',1))
    assert StateStore(settings.workspace).get(identifier)['status']=='needs_packaging'
    assert not json.loads(await read_artifact(settings,identifier,'approval.json'))['valid']
