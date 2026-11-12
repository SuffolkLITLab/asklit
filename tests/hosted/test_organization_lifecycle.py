import time
from sqlalchemy import select
from asklit.hosted.models import (Organization, Bot, Revision, UsageEvent, Job, Document,
    Membership, User, LocalAccount, DeletedOrganizationSpend)
from asklit.hosted.budget import committed_cost, month_start
from asklit.hosted.storage import object_store, cleanup_deleted_objects
from test_approval_requests import operator


def seed(hosted):
    client=operator(hosted)
    with hosted[1].transaction() as db:
        db.add(Bot(id='lifecycle-bot',org_id='school',owner_id='alice',title='Test',draft={}))
        db.flush()
        db.add(Revision(id='lifecycle-revision',bot_id='lifecycle-bot',config={},document_ids=[]))
        db.get(Bot,'lifecycle-bot').published_id='lifecycle-revision'
        db.add(UsageEvent(org_id='school',bot_id='lifecycle-bot',owner_id='alice',purpose='public',preset='default',model='fake',status='success',estimated_cost_usd=12.5))
    return client


def archive(client):
    r=client.patch('/api/operator/organizations/school',json={'archived':True})
    assert r.status_code==200,r.text


def erase(client,name='School'):
    return client.request('DELETE','/api/operator/organizations/school',json={'confirmation':name})


def test_archive_hides_and_blocks_then_restore(hosted):
    client=seed(hosted)
    with hosted[1].transaction() as db:
        db.add(Job(bot_id='lifecycle-bot',kind='export',payload={}))
    archive(client)
    assert 'school' not in [o['id'] for o in client.get('/api/operator/organizations').json()['organizations']]
    assert [o['id'] for o in client.get('/api/operator/organizations?status=archived').json()['organizations']]==['school']
    assert 'school' not in [o['id'] for o in client.get('/api/me').json()['organizations']]
    assert client.get('/api/bots').json()==[]
    from asklit.hosted.public import public_bot
    from fastapi import HTTPException
    import pytest
    with hosted[1].transaction() as db:
        with pytest.raises(HTTPException):public_bot(db,'lifecycle-bot')
        assert db.scalar(select(Job)).status=='failed'
    assert client.post('/api/operator/organizations/school/invitations',json={'email':'a@b.edu'}).status_code==409
    assert client.patch('/api/operator/organizations/school',json={'archived':False}).status_code==200
    assert 'school' in [o['id'] for o in client.get('/api/me').json()['organizations']]
    with hosted[1].transaction() as db:assert public_bot(db,'lifecycle-bot').id=='lifecycle-bot'


def test_permanent_delete_cleans_files_history_preserves_other_school_and_budget(hosted):
    client=seed(hosted);key='a'*32+'.txt';object_store(hosted[0]).put_bytes(key,b'sensitive file')
    with hosted[1].transaction() as db:
        db.add(Document(bot_id='lifecycle-bot',filename='file.txt',knowledgebase='kb',content_hash='hash',storage_key=key,size=14,embedding_model='fake'))
        db.add(LocalAccount(user_id='alice',username='alice@example.org',managed_org_id='school',password_hash='unused',must_change_password=False))
        before=committed_cost(db,month_start())
    assert erase(client).status_code==409
    archive(client)
    assert erase(client,'wrong').status_code==409
    response=erase(client);assert response.status_code==200,response.text
    cleanup_deleted_objects(hosted[0],hosted[1])
    assert not (hosted[0].storage/key).exists()
    with hosted[1].transaction() as db:
        assert db.get(Organization,'school') is None
        assert db.get(Organization,'other') is not None
        assert db.get(User,'alice') is None
        assert db.get(LocalAccount,'alice') is None
        assert not db.scalars(select(UsageEvent)).all()
        assert not db.scalars(select(Bot)).all()
        assert committed_cost(db,month_start())==before
        assert db.get(DeletedOrganizationSpend,month_start().strftime('%Y-%m')).amount==12.5
    assert erase(client).status_code==404


def test_permissions_inflight_and_storage_failure(hosted,monkeypatch):
    client=seed(hosted)
    assert hosted[4]('alice').patch('/api/operator/organizations/school',json={'archived':True}).status_code==403
    client=hosted[4]('owner');client.headers.pop('x-csrf-token')
    assert client.patch('/api/operator/organizations/school',json={'archived':True}).status_code==403
    client=hosted[4]('owner');archive(client)
    with hosted[1].transaction() as db:
        job=Job(bot_id='lifecycle-bot',kind='export',payload={},status='running',heartbeat=time.time());db.add(job);db.flush();jobid=job.id
    assert erase(client).status_code==409
    with hosted[1].transaction() as db:
        db.get(Job,jobid).status='done'
        db.get(Job,jobid).result={'storage_key':'b'*32+'.zip'}
    def fail(*args):raise RuntimeError('storage unavailable')
    monkeypatch.setattr('asklit.hosted.storage.FilesystemStorage.delete',fail)
    assert erase(client).status_code==200
    cleanup_deleted_objects(hosted[0],hosted[1])
    from asklit.hosted.models import PendingObjectDeletion
    with hosted[1].transaction() as db:
        assert db.get(Organization,'school') is None
        assert committed_cost(db,month_start())==12.5
        assert db.scalar(select(PendingObjectDeletion)) is not None
    monkeypatch.undo()
    cleanup_deleted_objects(hosted[0],hosted[1])
    with hosted[1].transaction() as db:assert db.scalar(select(PendingObjectDeletion)) is None


def test_cannot_delete_admin_login_org(hosted):
    client=seed(hosted)
    with hosted[1].transaction() as db:
        db.add(LocalAccount(user_id='owner',username='admin@example.org',managed_org_id='school',password_hash='unused',must_change_password=False))
    archive(client)
    r=erase(client);assert r.status_code==409 and 'Admin login' in r.text
