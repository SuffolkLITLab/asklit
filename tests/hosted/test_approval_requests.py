from sqlalchemy import select
from asklit.hosted.models import User, Audit


def operator(hosted):
    with hosted[1].transaction() as db:
        user=db.get(User,'owner');user.platform_admin=True;user.author_approved=True
    return hosted[4]('owner')


def test_search_filters_literal_wildcards_and_pages(hosted):
    client=operator(hosted)
    with hosted[1].transaction() as db:
        db.get(User,'alice').name='Alice Example'
        db.add(User(id='wild',name='100%_literal',email='literal@example.org'))
        for i in range(101): db.add(User(id=f'pending{i:03}',name='Queued author',email=f'pending{i}@example.org'))
    assert [x['id'] for x in client.get('/api/operator/accounts?q=ALICE%20EXAMPLE').json()['accounts']]==['alice']
    assert [x['id'] for x in client.get('/api/operator/accounts',params={'q':'%_'}).json()['accounts']]==['wild']
    first=client.get('/api/operator/accounts?q=Queued&status=pending').json()
    second=client.get('/api/operator/accounts?q=Queued&status=pending&page=2').json()
    assert len(first['accounts'])==100 and first['has_more']
    assert len(second['accounts'])==1 and not second['has_more']
    assert all(x['approved'] for x in client.get('/api/operator/accounts?status=approved').json()['accounts'])


def test_delete_request_requires_operator_csrf_and_pending_account(hosted):
    client=operator(hosted)
    path='/api/operator/accounts/alice/approval-request'
    assert hosted[4]('alice').delete(path).status_code==403
    client=hosted[4]('owner');client.headers.pop('x-csrf-token')
    assert client.delete(path).status_code==403
    client=hosted[4]('owner')
    assert client.delete('/api/operator/accounts/owner/approval-request').status_code==409
    with hosted[1].transaction() as db: db.get(User,'bob').author_approved=True
    assert client.delete('/api/operator/accounts/bob/approval-request').status_code==409
    assert client.delete(path).status_code==200
    assert client.get('/api/operator/accounts?q=alice').json()['accounts']==[]
    deleted=client.get('/api/operator/accounts?q=alice&status=deleted').json()['accounts']
    assert len(deleted)==1 and deleted[0]['request_deleted']
    with hosted[1].transaction() as db:
        assert not db.get(User,'alice').author_approved
        assert db.scalar(select(Audit).where(Audit.action=='hosting.request_delete'))
    hosted[0].require_author_approval=True
    assert hosted[4]('alice').get('/api/bots').status_code==403
    client=hosted[4]('owner')
    assert client.post(path).status_code==200
    assert len(client.get('/api/operator/accounts?q=alice').json()['accounts'])==1
    assert client.delete(path).status_code==200
    assert client.patch('/api/operator/accounts/alice',json={'approved':True}).status_code==200
    assert len(client.get('/api/operator/accounts?q=alice&status=approved').json()['accounts'])==1
