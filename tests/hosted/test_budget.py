"""Spending gate tests use fake providers; no billable model requests."""
from concurrent.futures import ThreadPoolExecutor
import time
import pytest
from sqlalchemy import select
from conftest import hosted
from test_api import new_bot, publish
from asklit.hosted.models import Bot, UsageEvent
from asklit.hosted.runtime import Runtime
from asklit.hosted.usage import meter, begin_call, finish_call
from asklit.hosted.budget import MonthlyBudgetExceeded, budget_report

MESSAGES = [{'role': 'user', 'content': 'hi'}]


def setup_budget(hosted, cap=.015):
    settings, store = hosted[:2]
    bot = new_bot(hosted)
    settings.monthly_ai_budget_usd = cap
    settings.models['default'].update(input_usd_per_million=1, output_usd_per_million=2)
    with store.transaction() as db:
        b = db.get(Bot, bot['id'])
    return settings, store, b, bot


def test_parallel_reservations_cannot_spend_same_balance(hosted):
    settings, store, bot, _ = setup_budget(hosted)
    def reserve(_):
        with meter(store, bot, 'preview'):
            try:
                return begin_call(settings, 'default', settings.models['default'], MESSAGES)
            except MonthlyBudgetExceeded:
                return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(6)))
    assert sum(x is not None for x in results) == 1
    with store.transaction() as db:
        assert budget_report(db, settings)['committed_usd'] <= settings.monthly_ai_budget_usd


def test_known_cost_releases_hold_unknown_failure_keeps_it(hosted):
    settings, store, bot, _ = setup_budget(hosted)
    with meter(store, bot, 'preview'):
        first = begin_call(settings, 'default', settings.models['default'], MESSAGES)
        finish_call(first, 'succeeded', {'input_tokens': 10, 'output_tokens': 10}, .1)
        second = begin_call(settings, 'default', settings.models['default'], MESSAGES)
        finish_call(second, 'failed', None, .1)
        with pytest.raises(MonthlyBudgetExceeded):
            begin_call(settings, 'default', settings.models['default'], MESSAGES)
    with store.transaction() as db:
        events=db.scalars(select(UsageEvent).order_by(UsageEvent.created)).all()
        assert events[1].estimated_cost_usd is None
        assert budget_report(db, settings)['committed_usd'] > events[0].estimated_cost_usd


def test_month_rollover_and_fail_closed_unpriced_or_unmetered(hosted):
    settings, store, bot, _ = setup_budget(hosted)
    with meter(store, bot, 'preview'):
        first=begin_call(settings,'default',settings.models['default'],MESSAGES)
    with store.transaction() as db:
        db.get(UsageEvent,first[1]).created=time.time()-40*86400
    with meter(store, bot, 'preview'):
        assert begin_call(settings,'default',settings.models['default'],MESSAGES)
    with pytest.raises(RuntimeError):
        begin_call(settings,'default',settings.models['default'],MESSAGES)
    with meter(store, bot, 'preview'), pytest.raises(Exception) as error:
        begin_call(settings,'default',{'model':'unpriced'},MESSAGES)
    assert error.value.status_code==503


def test_public_budget_error_is_explicit_and_provider_never_called(hosted):
    settings,store,bot,saved=setup_budget(hosted, .000001)
    publish(hosted,saved)
    client=hosted[3]
    conv=client.post(f'/api/public/bots/{bot.id}/conversations',json={'profile':'default'}).json()
    response=client.post(f"/api/public/conversations/{conv['id']}/messages",json={'message':'hello','request_id':'budget-test'},headers={'Authorization':'Bearer '+conv['token']})
    assert response.status_code==429
    assert response.json()['code']=='monthly_ai_budget_exceeded'
    assert 'spending limit' in response.json()['detail']
    assert not hosted[5]


def test_worker_stops_matrix_with_billing_error(hosted):
    from test_evaluation import matrix_bot
    from asklit.hosted.worker import run_one
    from asklit.hosted.models import Job
    bot=matrix_bot(hosted)
    settings,store,runtime,client=hosted[:4]
    settings.monthly_ai_budget_usd=.000001
    for model in settings.models.values():
        model.update(input_usd_per_million=1, output_usd_per_million=2)
    queued=client.post(f"/api/bots/{bot['id']}/evaluate")
    assert queued.status_code==200
    run_one(store,settings,runtime)
    with store.transaction() as db:
        job=db.get(Job,queued.json()['id'])
        assert job.status=='failed'
        assert job.result['code']=='monthly_ai_budget_exceeded'
        assert 'spending limit' in job.result['error']
    assert not hosted[5]


@pytest.mark.parametrize('code,reference,status', [('AccessDeniedException','ai_provider_access_denied',503),('ServiceQuotaExceededException','ai_provider_quota_exceeded',429)])
def test_provider_quota_and_budget_backstop_errors_are_safe_and_visible(hosted,monkeypatch,code,reference,status):
    import boto3
    from botocore.exceptions import ClientError
    settings,store,bot,saved=setup_budget(hosted,100)
    settings.models['default']['provider']='bedrock'
    class Provider:
        def converse(self,**kwargs):
            raise ClientError({'Error':{'Code':code,'Message':'sensitive-provider-detail'}},'Converse')
    monkeypatch.setattr(boto3.session.Session,'client',lambda *a,**k:Provider())
    hosted[2].completion=None
    response=hosted[3].post(f'/api/bots/{bot.id}/preview',json={'message':'hello','profile':'default'})
    assert response.status_code==status,response.text
    assert response.json()['code']==reference
    assert reference in response.json()['detail']
    assert 'sensitive-provider-detail' not in response.text


def test_unknown_historical_cost_is_visible_and_cannot_be_ignored(hosted):
    settings,store,bot,_=setup_budget(hosted,100)
    with store.transaction() as db:
        db.add(UsageEvent(org_id=bot.org_id,bot_id=bot.id,owner_id=bot.owner_id,purpose='preview',preset='old',model='old',status='failed'))
    with store.transaction() as db:
        report=budget_report(db,settings)
        assert report['legacy_unknown_calls']==1
        assert report['committed_usd']==1
        assert report['estimated_spend_usd']==0


def test_conservative_rates_hold_more_than_standard_context_charge(hosted):
    settings,store,bot,_=setup_budget(hosted,100)
    model=settings.models['default']
    model.update(budget_input_usd_per_million=5,budget_output_usd_per_million=10)
    with meter(store,bot,'preview'):
        handle=begin_call(settings,'default',model,MESSAGES)
        with store.transaction() as db:
            reserved=budget_report(db,settings)['committed_usd']
        finish_call(handle,'succeeded',{'input_tokens':10,'output_tokens':10},.1)
    with store.transaction() as db:
        assert reserved>budget_report(db,settings)['committed_usd']
        assert db.get(UsageEvent,handle[1]).estimated_cost_usd==pytest.approx(.00003)


def test_bedrock_forwards_operator_thinking_configuration(hosted,monkeypatch):
    import boto3
    observed={}
    class Provider:
        def converse(self,**kwargs):
            observed.update(kwargs)
            return {'output':{'message':{'content':[{'text':'Violet'}]}},'usage':{'inputTokens':9,'outputTokens':2}}
    monkeypatch.setattr(boto3.session.Session,'client',lambda *a,**k:Provider())
    answer,usage=hosted[2].bedrock(MESSAGES,{'model':'test-haiku','additional_model_request_fields':{'thinking':{'type':'disabled'}}})
    assert observed['additionalModelRequestFields']=={'thinking':{'type':'disabled'}}
    assert answer=='Violet' and usage['output_tokens']==2
