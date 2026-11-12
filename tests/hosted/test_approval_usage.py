"""Hosting approval, tenant boundaries, and durable provider accounting."""
import pytest
from sqlalchemy import select
from asklit.hosted.models import User, UsageEvent, Bot
from asklit.hosted.runtime import Runtime
from asklit.hosted.usage import meter
from test_api import new_bot, publish


def test_approval_is_operator_only_and_suspension_unpublishes(hosted):
    settings,store,_,client,as_user,_=hosted
    bot=new_bot(hosted);publish(hosted,bot)
    settings.require_author_approval=True
    with store.transaction() as db:
        owner=db.get(User,'owner');owner.author_approved=True;owner.platform_admin=True
    assert as_user('alice').get('/api/me').json()['author_approved'] is False
    assert as_user('alice').post('/api/bots',json={'org_id':'school','title':'Blocked'}).status_code==403
    assert as_user('alice').get('/api/operator/accounts').status_code==403
    assert as_user('owner').patch('/api/operator/accounts/alice',json={'approved':True,'platform_admin':True}).status_code==422
    assert as_user('owner').patch('/api/operator/accounts/alice',json={'approved':True}).status_code==200
    assert as_user('alice').get('/api/bots').status_code==200
    assert as_user('alice').patch('/api/operator/accounts/bob',json={'approved':True}).status_code==403
    assert as_user('owner').patch('/api/operator/accounts/alice',json={'approved':False}).status_code==200
    assert client.get('/api/public/bots/'+bot['id']).status_code==404
    assert as_user('alice').get('/api/bots').status_code==403
    assert as_user('owner').patch('/api/operator/accounts/owner',json={'approved':False}).status_code==409
    assert as_user('owner').patch('/api/operator/accounts/alice',json={'approved':True}).status_code==200
    assert client.get('/api/public/bots/'+bot['id']).status_code==404  # explicit republish required


def test_usage_records_tokens_rates_failures_and_tenant_boundaries(hosted,monkeypatch):
    import boto3
    settings,store,_,client,as_user,_=hosted
    bot=new_bot(hosted)
    settings.models['bedrock']={'model':'us.amazon.nova-micro-v1:0','provider':'bedrock','input_usd_per_million':1,'output_usd_per_million':2}
    class Provider:
        def converse(self,**kwargs):
            assert kwargs['modelId']=='us.amazon.nova-micro-v1:0'
            assert kwargs['system']==[{'text':'system'}]
            return {'output':{'message':{'content':[{'text':'Answer'}]}},'usage':{'inputTokens':100,'outputTokens':50}}
    monkeypatch.setattr(boto3.session.Session,'client',lambda *a,**k:Provider())
    runtime=Runtime(settings)
    with store.transaction() as db: b=db.get(Bot,bot['id'])
    with meter(store,b,'preview'):
        assert runtime.call([{'role':'system','content':'system'},{'role':'user','content':'hi'}],'bedrock')=='Answer'
    settings.models['bedrock']['input_usd_per_million']=99
    with store.transaction() as db:
        event=db.scalar(select(UsageEvent));assert event.input_tokens==100;assert event.output_tokens==50
        assert event.estimated_cost_usd==pytest.approx(.0002);assert event.input_rate==1
    def failed(*a,**k):raise RuntimeError('secret provider message')
    monkeypatch.setattr(boto3.session.Session,'client',failed)
    with meter(store,b,'public_chat'),pytest.raises(RuntimeError):runtime.call([],'bedrock')
    report=as_user('owner').get('/api/organizations/school/usage').json()
    assert report['totals']['requests']==2
    assert report['totals']['failed']==1
    assert report['totals']['unknown_cost_requests']==1
    assert report['totals']['estimated_cost_usd']==pytest.approx(.0002)
    assert 'secret provider message' not in str(report)
    assert as_user('outsider').get('/api/organizations/school/usage').status_code==404
    assert as_user('alice').get('/api/organizations/school/usage').status_code==404
    assert as_user('owner').get('/api/operator/usage').status_code==403
    with store.transaction() as db:db.get(User,'owner').platform_admin=True
    assert as_user('owner').get('/api/operator/usage').json()['totals']['requests']==2
    assert as_user('owner').get('/api/operator/usage?days=10000').status_code==422


def test_meter_fails_closed_for_unapproved_author_and_missing_context(hosted):
    settings,store,_,_,_,_=hosted
    bot=new_bot(hosted)
    settings.require_author_approval=True
    runtime=Runtime(settings,completion=lambda *a:'never')
    with pytest.raises(RuntimeError):runtime.call([],'default')
    with store.transaction() as db:b=db.get(Bot,bot['id'])
    with meter(store,b,'evaluation'),pytest.raises(Exception) as error:runtime.call([],'default')
    assert error.value.status_code==403
    with store.transaction() as db:assert db.scalar(select(UsageEvent)) is None


def test_other_admin_cannot_republish_suspended_authors_revision(hosted):
    settings,store,_,_,as_user,_=hosted
    bot=new_bot(hosted);published=publish(hosted,bot)
    settings.require_author_approval=True
    with store.transaction() as db:
        admin=db.get(User,'owner');admin.author_approved=True;admin.platform_admin=True
    client=as_user('owner')
    assert client.patch('/api/operator/accounts/alice',json={'approved':False}).status_code==200
    assert client.post(f"/api/bots/{bot['id']}/revisions/{published['revision_id']}/activate").status_code==403
    assert client.post(f"/api/bots/{bot['id']}/publish",json={'version':bot['version']}).status_code==403


def test_public_metadata_identifies_publisher_even_when_locked(hosted):
    bot=new_bot(hosted);publish(hosted,bot)
    with hosted[1].transaction() as db:
        db.get(Bot,bot['id']).access_mode='password'
    response=hosted[3].get('/api/public/bots/'+bot['id'])
    data=response.json()
    assert data['locked'] is True
    assert data['id']==bot['id']
    assert data['organization']['name']=='School'
    assert data['title']=='Protected AskLit'


def test_matrix_worker_meters_answers_and_judges_with_approval_required(hosted):
    import json
    from test_evaluation import matrix_bot
    from asklit.hosted.worker import run_one
    from asklit.hosted.models import Job
    bot=matrix_bot(hosted)
    settings,store,runtime,client=hosted[:4]
    settings.require_author_approval=True
    with store.transaction() as db:db.get(User,'alice').author_approved=True
    runtime.completion=lambda messages,preset:json.dumps({'score':1,'reason':'Correct'}) if preset=='judge' else 'Grounded answer'
    queued=client.post(f"/api/bots/{bot['id']}/evaluate")
    assert queued.status_code==200
    run_one(store,settings,runtime)
    with store.transaction() as db:
        assert db.get(Job,queued.json()['id']).status=='succeeded'
        events=db.scalars(select(UsageEvent)).all()
        assert len(events)==64
        assert sum(event.preset=='judge' for event in events)==32
        assert all(event.owner_id=='alice' and event.status=='succeeded' and event.purpose=='evaluation' for event in events)


@pytest.mark.parametrize('preset', ['gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.1-sol'])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('input_tokens,cached', [(100, 0), (272001, 0), (100, 20)])
def test_frontier_presets_meter_converse_without_underpricing(hosted, monkeypatch, preset, input_tokens, cached, empty):
    import json
    from pathlib import Path
    import boto3
    settings, store = hosted[:2]
    bot = new_bot(hosted)
    settings.models.update(json.loads(Path('docs/bedrock-models.json').read_text()))
    class Provider:
        def converse(self, **kwargs):
            assert kwargs['modelId'] == 'us.openai.' + preset
            assert kwargs['inferenceConfig']['maxTokens'] == 1500
            return {'output': {'message': {'content': [
                {'reasoningContent': {'redactedContent': b'private'}}, {'text': '' if empty else 'violet'}
            ]}}, 'usage': {'inputTokens': input_tokens, 'outputTokens': 17, 'cacheReadInputTokens': cached}}
    monkeypatch.setattr(boto3.session.Session, 'client', lambda *a, **k: Provider())
    with store.transaction() as db:
        b = db.get(Bot, bot['id'])
    with meter(store, b, 'preview'):
        if empty:
            with pytest.raises(RuntimeError, match='no answer'):
                Runtime(settings).call([{'role': 'user', 'content': 'Reply violet'}], preset)
        else:
            assert Runtime(settings).call([{'role': 'user', 'content': 'Reply violet'}], preset) == 'violet'
    with store.transaction() as db:
        event = db.scalar(select(UsageEvent))
        assert event.status == ('failed' if empty else 'succeeded')
        assert event.input_tokens == input_tokens
        assert event.output_tokens == 17
        if cached or input_tokens > 272000:
            assert event.estimated_cost_usd is None
        else:
            model = settings.models[preset]
            assert event.estimated_cost_usd == pytest.approx((input_tokens * model['input_usd_per_million'] + 17 * model['output_usd_per_million']) / 1_000_000)
