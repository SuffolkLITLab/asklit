import re
from fastapi.testclient import TestClient
from sqlalchemy import select
from asklit.hosted.models import LocalAccount, Membership
from asklit.hosted.local_accounts import bootstrap_admin

PASSWORD = 'correct horse battery staple'
NEW = 'another strong local password'


def create(hosted, email='student@example.org', role='member'):
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
        assert 'password' not in str(me)
        assert c.get('/api/organizations/school/local-accounts').status_code==404
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
