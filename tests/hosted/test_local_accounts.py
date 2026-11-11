import re
from fastapi.testclient import TestClient
from sqlalchemy import select
from asklit.hosted.models import LocalAccount, Membership, Organization
from asklit.hosted.local_accounts import bootstrap_admin

PASSWORD = 'correct horse battery staple'
NEW = 'another strong local password'


def create(hosted, email='student@example.org', role='member'):
    with hosted[1].transaction() as db:
        db.get(Organization,'school').local_accounts_enabled=True
    r=hosted[4]('owner').post('/api/organizations/school/local-accounts',json={'email':email,'name':'Student','password':PASSWORD,'role':role})
    assert r.status_code==200,r.text
    return r.json()


def sign_in(client,email,password=PASSWORD):
    page=client.get('/')
    csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
    return client.post('/auth/password/login',data={'email':email,'password':password,'csrf':csrf},follow_redirects=False)


def test_local_login_csrf_normalization_and_no_oauth_identity_merge(hosted):
    account=create(hosted,' ALICE@example.org ')
    assert account['id']!='alice'
    with TestClient(hosted[3].app) as c:
        assert c.post('/auth/password/login',data={'email':'alice@example.org','password':PASSWORD,'csrf':'wrong'}).status_code==403
        assert sign_in(c,'ALICE@example.org').status_code==303
        me=c.get('/api/me').json()
        assert me['id']==account['id'] and me['local_account']
        assert me['organizations'][0]['role']=='member'
        assert PASSWORD not in str(me) and 'password_hash' not in me
        assert me['must_change_password']
        assert c.get('/api/organizations/school/local-accounts').status_code==403
        assert c.post('/auth/local/login',data={}).status_code in [404,405]
    with hosted[1].transaction() as db:
        assert db.get(LocalAccount,account['id']).password_hash != PASSWORD


def test_account_management_roles_cross_org_csrf_and_disabled_sessions(hosted):
    account=create(hosted)
    base='/api/organizations/school/local-accounts/'+account['id']
    assert hosted[4]('outsider').patch(base,json={'enabled':False}).status_code==404
    assert hosted[4]('alice').get('/api/organizations/school/local-accounts').status_code==404
    with TestClient(hosted[3].app) as c:
        sign_in(c,account['email'])
        owner=hosted[4]('owner')
        owner.headers.pop('x-csrf-token')
        assert owner.patch(base,json={'enabled':False}).status_code==403
        owner.headers['x-csrf-token']='csrf'
        assert owner.patch(base,json={'enabled':False}).status_code==200
        assert c.get('/api/me').status_code==401
        assert sign_in(c,account['email']).headers['location']=='/?login_error=1'
        assert owner.patch(base,json={'enabled':True}).status_code==200
        sign_in(c,account['email'])
        assert c.get('/api/me').status_code==200
    with hosted[1].transaction() as db:
        db.get(Membership,('school','alice')).role='admin'
    assert hosted[4]('alice').post('/api/organizations/school/local-accounts',json={'email':'admin@example.org','name':'Admin','password':PASSWORD,'role':'admin'}).status_code==403


def test_reset_and_self_change_revoke_sessions(hosted):
    account=create(hosted)
    path='/api/organizations/school/local-accounts/'+account['id']+'/password'
    with TestClient(hosted[3].app) as c:
        sign_in(c,account['email'])
        assert hosted[4]('owner').post(path,json={'password':NEW}).status_code==200
        assert c.get('/api/me').status_code==401
        assert sign_in(c,account['email']).headers['location']=='/?login_error=1'
        sign_in(c,account['email'],NEW)
        c.headers['x-csrf-token']=c.get('/api/me').json()['csrf']
        assert c.post('/api/account/password',json={'current_password':'wrong','password':PASSWORD}).status_code==400
        assert c.post('/api/account/password',json={'current_password':NEW,'password':PASSWORD}).status_code==200
        assert c.get('/api/me').status_code==401
        sign_in(c,account['email'])
        assert c.get('/api/me').status_code==200


def test_password_policy_and_committed_login_limits(hosted):
    c=hosted[4]('owner')
    assert c.post('/api/organizations/school/local-accounts',json={'email':'x@y.org','name':'X','password':'short'}).status_code==422
    with TestClient(c.app) as anonymous:
        for _ in range(10):
            assert sign_in(anonymous,'missing@example.org').status_code==303
        assert sign_in(anonymous,'missing@example.org').status_code==429


def test_bootstrap_owner_is_not_disabled_or_silently_reset(hosted):
    uid,org=bootstrap_admin(hosted[1],'lab@example.org','Lab','LIT Lab','lit',PASSWORD)
    with TestClient(hosted[3].app) as c:
        sign_in(c,'lab@example.org');me=c.get('/api/me').json();c.headers['x-csrf-token']=me['csrf']
        base=f'/api/organizations/{org}/local-accounts/{uid}'
        assert c.patch(base,json={'enabled':False}).status_code==409
        assert c.post(base+'/password',json={'password':NEW}).status_code==409
    import pytest
    with pytest.raises(ValueError):bootstrap_admin(hosted[1],'lab@example.org','Lab','LIT Lab','lit',NEW)


def test_local_membership_is_explicit_and_disabled_users_cannot_be_owners(hosted):
    from asklit.hosted.models import Organization
    from asklit.hosted.access import matches_organization
    account=create(hosted)
    with hosted[1].transaction() as db:
        org=db.get(Organization,'school')
        assert matches_organization(db,org,account['id'],hosted[0])
        other=db.get(Organization,'other');other.allowed_domains=['example.org']
        assert not matches_organization(db,other,account['id'],hosted[0])
    c=hosted[4]('owner')
    assert c.patch('/api/organizations/school/local-accounts/'+account['id'],json={'enabled':False}).status_code==200
    assert c.patch('/api/organizations/school/members/'+account['id'],json={'role':'owner'}).status_code==409


def test_provisioning_requires_operator_enablement(hosted):
    c=hosted[4]('owner')
    assert c.post('/api/organizations/school/local-accounts',json={'email':'x@example.org','name':'X','password':PASSWORD}).status_code==403


def test_forced_password_change_and_organization_isolation(hosted):
    account=create(hosted)
    invite=hosted[4]('outsider').post('/api/organizations/other/invitations',json={'role':'member'}).json()['url'].split('invite=')[1]
    with TestClient(hosted[3].app) as c:
        sign_in(c,account['email'])
        c.headers['x-csrf-token']=c.get('/api/me').json()['csrf']
        assert c.get('/api/bots').status_code==403
        assert c.post('/api/account/password',json={'current_password':PASSWORD,'password':PASSWORD}).status_code==400
        assert c.post('/api/account/password',json={'current_password':PASSWORD,'password':NEW}).status_code==200
        sign_in(c,account['email'],NEW)
        me=c.get('/api/me').json();c.headers['x-csrf-token']=me['csrf']
        assert me['must_change_password'] is False
        assert c.get('/api/bots').status_code==200
        assert c.post('/api/organizations',json={'name':'Escape'}).status_code==403
        assert c.post('/api/invitations/accept',json={'token':invite}).status_code==403
    owner=hosted[4]('owner')
    assert owner.delete('/api/organizations/school/members/'+account['id']).status_code==409
    with hosted[1].transaction() as db:
        db.delete(db.get(Membership,('school',account['id'])))
    assert owner.post('/api/organizations/school/local-accounts/'+account['id']+'/password',json={'password':NEW}).status_code==404


def test_bad_attempts_from_one_ip_do_not_block_another(hosted,monkeypatch):
    monkeypatch.setattr('asklit.hosted.local_accounts.time.time',lambda:1794400000.0)
    # Existing fixture sessions use real time and remain unexpired.
    with TestClient(hosted[3].app,client=('192.0.2.1',5000)) as a, TestClient(hosted[3].app,client=('192.0.2.2',5000)) as b:
        for _ in range(10):assert sign_in(a,'nobody@example.org').status_code==303
        assert sign_in(a,'nobody@example.org').status_code==429
        assert sign_in(b,'nobody@example.org').status_code==303
        assert sign_in(b,'bad\x00@example.org').status_code==303


def test_fly_client_address_ignores_spoofed_forwarded_chain():
    import asyncio
    from asklit.hosted.app import FlyClientAddress
    captured=[]
    async def app(scope,receive,send):captured.append(scope['client'][0])
    middleware=FlyClientAddress(app)
    for headers in [[(b'fly-client-ip',b'192.0.2.10'),(b'x-forwarded-for',b'198.51.100.42')],[(b'x-forwarded-for',b'198.51.100.42')]]:
        asyncio.run(middleware({'type':'http','headers':headers,'client':('proxy',123)},None,None))
    assert captured==['192.0.2.10','unknown']


def test_password_reset_during_login_rejects_stale_password(hosted,monkeypatch):
    import asklit.hosted.local_accounts as accounts
    account=create(hosted)
    original=accounts.password_matches
    def verify_then_reset(value,stored):
        valid=original(value,stored)
        r=hosted[4]('owner').post('/api/organizations/school/local-accounts/'+account['id']+'/password',json={'password':NEW})
        assert r.status_code==200
        return valid
    monkeypatch.setattr(accounts,'password_matches',verify_then_reset)
    with TestClient(hosted[3].app) as c:
        assert sign_in(c,account['email']).headers['location']=='/?login_error=1'
        assert c.get('/api/me').status_code==401


def test_reset_refuses_legacy_cross_organization_account(hosted):
    account=create(hosted)
    with hosted[1].transaction() as db:
        db.add(Membership(org_id='other',user_id=account['id'],role='member'))
    r=hosted[4]('owner').post('/api/organizations/school/local-accounts/'+account['id']+'/password',json={'password':NEW})
    assert r.status_code==403
