import re
from datetime import datetime, timezone
import pytest
from sqlalchemy import select, func
from asklit.hosted.models import User, LocalAccount, Organization, Membership
from asklit.hosted.settings import Settings


def signup(client, email='new@example.org', **extra):
    page=client.get('/')
    csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
    body={'email':email,'name':'New author','password':'a-safe-test-password','csrf':csrf,**extra}
    return client.post('/api/register',json=body)


def test_trial_registration_private_workspace_and_expiry(hosted):
    settings,store,_,client=hosted[:4]
    settings.require_author_approval=True;settings.open_registration_until='2099-01-01'
    assert 'id="open-registration"' in client.get('/').text
    assert 'no hosting approval or fee' in client.get('/terms').text
    r=signup(client);assert r.status_code==200,r.text
    me=client.get('/api/me').json();assert me['author_approved'] and not me['approval_required'] and not me['platform_admin']
    assert len(me['organizations'])==1 and me['organizations'][0]['role']=='owner'
    with store.transaction() as db:
        assert not db.get(Organization,me['organizations'][0]['id']).local_accounts_enabled
        assert not db.get(User,me['id']).author_approved
        assert db.get(LocalAccount,me['id']).managed_org_id==me['organizations'][0]['id']
        assert db.get(Membership,('school',me['id'])) is None
    assert client.get('/api/bots').status_code==200
    settings.open_registration_until='2000-01-01'
    assert client.get('/api/bots').status_code==403
    assert not client.get('/api/me').json()['author_approved']
    assert 'id="open-registration"' not in client.get('/').text
    assert 'Hosted author accounts require LIT Lab approval' in client.get('/terms').text
    assert signup(client,'after@example.org').status_code==403
    with store.transaction() as db:
        db.get(User,me['id']).author_approved=True
    assert client.get('/api/bots').status_code==200


def test_signup_csrf_duplicate_and_no_privilege_fields(hosted):
    client=hosted[3]
    assert signup(client,csrf='wrong').status_code==403
    assert signup(client,role='owner').status_code==422
    assert signup(client,org_id='school').status_code==422
    assert signup(client,platform_admin=True).status_code==422
    assert signup(client,name='   ').status_code==422
    assert signup(client).status_code==200
    with hosted[1].transaction() as db:before=db.scalar(select(func.count()).select_from(Organization))
    assert signup(client,'NEW@EXAMPLE.ORG').status_code==409
    with hosted[1].transaction() as db:assert db.scalar(select(func.count()).select_from(Organization))==before


def test_registration_rate_limit_is_shared_across_emails(hosted,monkeypatch):
    monkeypatch.setattr('asklit.hosted.registration.password_hash',lambda value:'test-only-hash')
    client=hosted[3]
    for i in range(10):assert signup(client,f'new{i}@example.org').status_code==200
    assert signup(client,'eleventh@example.org').status_code==429


def test_trial_expires_at_utc_boundary_and_boolean_override(monkeypatch):
    class Clock:
        current=datetime(2026,12,31,23,59,59,tzinfo=timezone.utc)
        @classmethod
        def now(cls,tz):return cls.current
    monkeypatch.setattr('asklit.hosted.settings.datetime',Clock)
    settings=Settings(database_url='unused',secret_key='secret',require_author_approval=True,open_registration_until='2027-01-01')
    assert not settings.approval_required
    Clock.current=datetime(2027,1,1,tzinfo=timezone.utc)
    assert settings.approval_required
    settings.open_registration_until='2027-02-01';assert not settings.approval_required
    settings.open_registration_until='';assert settings.approval_required
    settings.require_author_approval=False;assert not settings.approval_required
    with pytest.raises(ValueError,match='YYYY-MM-DD'):
        Settings(database_url='unused',secret_key='secret',open_registration_until='tomorrow')


def test_indefinite_open_registration_does_not_claim_date_expiry(hosted):
    settings=hosted[0];settings.require_author_approval=False;settings.open_registration_until='2000-01-01'
    assert not settings.approval_required
    terms=hosted[3].get('/terms').text
    assert 'no hosting approval or fee' in terms
    assert 'scheduled to end' not in terms
    assert signup(hosted[3]).status_code==200
