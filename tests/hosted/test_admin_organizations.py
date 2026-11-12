import re
import time
from sqlalchemy import select
from fastapi.testclient import TestClient
from asklit.hosted.models import User, Membership, LocalAccount, Invitation, Organization, UsageEvent, Bot
from test_approval_requests import operator


def provision(hosted):
    client=operator(hosted)
    r=client.post('/api/operator/organizations',json={'name':'Example Law School','slug':'example-law'})
    assert r.status_code==200,r.text
    return r.json()


def invite(hosted,org,email='prof@example.edu',role='member'):
    response=hosted[4]('owner').post(f"/api/operator/organizations/{org['id']}/invitations",json={'email':email,'role':role})
    assert response.status_code==200,response.text
    value=response.json();return value,value['url'].split('#invite=')[1]


def test_operator_can_provision_schools_without_joining_them(hosted):
    org=provision(hosted);client=hosted[4]('owner')
    rows=client.get('/api/operator/organizations?q=EXAMPLE').json()['organizations']
    assert len(rows)==1 and rows[0]['members']==0
    with hosted[1].transaction() as db:assert db.get(Membership,(org['id'],'owner')) is None
    assert client.post('/api/operator/organizations',json={'name':'Duplicate','slug':'example-law'}).status_code==409
    assert client.post('/api/operator/organizations',json={'name':'Reserved','slug':'auth'}).status_code==422
    assert client.post('/api/operator/organizations',json={'name':'Invalid','slug':'../bad'}).status_code==422
    client.headers.pop('x-csrf-token')
    assert client.post('/api/operator/organizations',json={'name':'CSRF','slug':'csrf'}).status_code==403
    client=hosted[4]('alice')
    assert client.get('/api/operator/organizations').status_code==403
    assert client.post('/api/operator/organizations',json={'name':'Forbidden','slug':'forbidden'}).status_code==403
    assert client.get(f"/api/operator/organizations/{org['id']}/usage").status_code==403


def test_named_operator_invite_approves_new_local_account_in_correct_school(hosted):
    org=provision(hosted);value,raw=invite(hosted,org,role='owner')
    hosted[0].require_author_approval=True
    with TestClient(hosted[3].app) as visitor:
        page=visitor.get('/');csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        preview=visitor.post('/api/invitations/inspect',json={'token':raw}).json()
        assert preview['organization']==org['name'] and preview['hosting_approved']
        body={'token':raw,'email':'prof@example.edu','name':'Professor Example','password':'a-strong-example-password','csrf':csrf}
        assert visitor.post('/api/invitations/register',json={**body,'csrf':'wrong'}).status_code==403
        assert visitor.post('/api/invitations/register',json={**body,'email':'other@example.edu'}).status_code==403
        assert visitor.post('/api/invitations/register',json={**body,'role':'owner'}).status_code==422
        response=visitor.post('/api/invitations/register',json=body)
        assert response.status_code==200,response.text
        me=visitor.get('/api/me').json()
        assert me['author_approved'] and not me['platform_admin'] and not me['must_change_password']
        assert [o['id'] for o in me['organizations']]==[org['id']]
        assert me['organizations'][0]['role']=='owner'
        assert visitor.get('/api/bots').status_code==200
        assert visitor.post('/api/invitations/inspect',json={'token':raw}).status_code==404
        with hosted[1].transaction() as db:assert db.get(LocalAccount,me['id']).managed_org_id==org['id']
    listed=hosted[4]('owner').get('/api/operator/organizations?q=example-law').json()['organizations'][0]
    assert listed['members']==1 and listed['approved_members']==1


def test_pending_existing_user_accepts_only_matching_operator_invite(hosted):
    org=provision(hosted);_,raw=invite(hosted,org,email='alice@example.org')
    hosted[0].require_author_approval=True
    assert hosted[4]('bob').post('/api/invitations/accept',json={'token':raw}).status_code==403
    alice=hosted[4]('alice')
    assert alice.get('/api/bots').status_code==403
    assert alice.post('/api/invitations/accept',json={'token':raw}).status_code==200
    assert alice.get('/api/me').json()['author_approved']
    with hosted[1].transaction() as db:assert db.get(Membership,(org['id'],'alice')).role=='member'
    assert alice.post('/api/invitations/accept',json={'token':raw}).status_code==404


def test_revoked_expired_and_regular_invites_cannot_grant_hosting(hosted):
    org=provision(hosted);value,raw=invite(hosted,org)
    client=hosted[4]('owner')
    assert client.delete(f"/api/operator/organizations/{org['id']}/invitations/{value['id']}").status_code==200
    assert client.post('/api/invitations/inspect',json={'token':raw}).status_code==404
    _,raw=invite(hosted,org)
    with hosted[1].transaction() as db:
        from asklit.hosted.security import digest
        db.get(Invitation,digest(raw)).expires=time.time()-1
    assert client.post('/api/invitations/inspect',json={'token':raw}).status_code==404
    regular=client.post('/api/organizations/school/invitations',json={'role':'member'}).json()['url'].split('#invite=')[1]
    hosted[0].require_author_approval=True
    bob=hosted[4]('bob');assert bob.post('/api/invitations/accept',json={'token':regular}).status_code==200
    assert not bob.get('/api/me').json()['author_approved']
    assert bob.get('/api/bots').status_code==403


def test_school_usage_aggregates_all_members_not_other_schools(hosted):
    from test_api import new_bot
    bot=new_bot(hosted);operator(hosted)
    with hosted[1].transaction() as db:
        db.add(UsageEvent(org_id='school',bot_id=bot['id'],owner_id='alice',purpose='preview',preset='fake',model='fake',status='succeeded',input_tokens=100,output_tokens=50,estimated_cost_usd=.25))
    rows=hosted[4]('owner').get('/api/operator/organizations').json()['organizations']
    school=next(r for r in rows if r['id']=='school');other=next(r for r in rows if r['id']=='other')
    assert school['requests']==1 and school['estimated_cost_usd']==.25
    assert other['requests']==0 and other['estimated_cost_usd']==0
    detail=hosted[4]('owner').get('/api/operator/organizations/school/usage').json()
    assert detail['totals']['estimated_cost_usd']==.25


def test_existing_members_are_not_silently_promoted_or_invited_again(hosted):
    org=provision(hosted)
    first,raw=invite(hosted,org,email='alice@example.org',role='owner')
    with hosted[1].transaction() as db:db.add(Membership(org_id=org['id'],user_id='alice',role='member'))
    client=hosted[4]('owner')
    assert client.post(f"/api/operator/organizations/{org['id']}/invitations",json={'email':'alice@example.org','role':'owner'}).status_code==409
    alice=hosted[4]('alice')
    assert alice.post('/api/invitations/accept',json={'token':raw}).status_code==409
    with hosted[1].transaction() as db:
        assert not db.get(Invitation,first['id']).used
        assert db.get(Membership,(org['id'],'alice')).role=='member'


def test_regular_invitation_cannot_create_local_accounts(hosted):
    client=operator(hosted)
    with hosted[1].transaction() as db:db.get(Organization,'school').local_accounts_enabled=True
    raw=client.post('/api/organizations/school/invitations',json={'role':'admin'}).json()['url'].split('#invite=')[1]
    with TestClient(hosted[3].app) as visitor:
        page=visitor.get('/');csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        assert not visitor.post('/api/invitations/inspect',json={'token':raw}).json()['local_signup']
        r=visitor.post('/api/invitations/register',json={'token':raw,'email':'new@example.org','name':'No signup','password':'a-long-example-password','csrf':csrf})
        assert r.status_code==403
