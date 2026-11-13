from types import SimpleNamespace
from sqlalchemy import select
from asklit.hosted.models import Bot, Document, Revision
from asklit.hosted.runtime import Runtime
from asklit.hosted.schemas import BotConfig
from asklit.hosted.portability import read_workspace
from test_api import new_bot,publish


def save(hosted,bot,**values):
    r=hosted[4]('alice').put('/api/bots/'+bot['id'],json={'version':bot['version'],'config':bot['config'],**values})
    assert r.status_code==200,r.text
    return r.json()


def test_private_instructor_notes_and_long_instructions(hosted):
    bot=new_bot(hosted);bot['config']['student_instructions']='Student briefing.\n'*500
    bot=save(hosted,bot,instructor_notes='INSTRUCTOR-ONLY-SENTINEL',instructor_shared=True,instructor_password='private-instructor-password')
    publish(hosted,bot);c=hosted[3];public='/api/public/bots/'+bot['id']
    meta=c.get(public).json()
    assert len(meta['student_instructions'])>2000 and meta['instructor_notes_available']
    assert 'INSTRUCTOR-ONLY' not in str(meta) and 'private-instructor-password' not in str(bot)
    with hosted[1].transaction() as db:
        row=db.get(Bot,bot['id']);assert row.instructor_password_hash!='private-instructor-password'
        assert 'INSTRUCTOR-ONLY' not in str(db.get(Revision,row.published_id).config)
    assert c.get(public+'/instructor-notes').status_code==405
    assert c.post(public+'/instructor-notes',json={'password':'wrong'}).status_code==403
    result=c.post(public+'/instructor-notes',json={'password':'private-instructor-password'})
    assert result.json()=={'notes':'INSTRUCTOR-ONLY-SENTINEL'} and result.headers['cache-control']=='no-store'
    workspace=c.get('/api/bots/'+bot['id']+'/workspace').text
    assert 'INSTRUCTOR-ONLY' not in workspace and 'private-instructor-password' not in workspace
    imported,_=read_workspace(workspace,hosted[0]);assert imported['student_instructions']==bot['config']['student_instructions']
    assert hosted[4]('bob').get('/api/bots/'+bot['id']).status_code==404
    bot=save(hosted,bot,instructor_shared=False)
    assert not hosted[3].get(public).json()['instructor_notes_available']
    assert hosted[3].post(public+'/instructor-notes',json={'password':'private-instructor-password'}).status_code==403


def test_citation_hiding_applies_to_model_output_and_old_turn_replays(hosted):
    bot=new_bot(hosted)
    with hosted[1].transaction() as db:
        db.add(Document(bot_id=bot['id'],filename='confidential.txt',knowledgebase='default',content_hash='x',storage_key='a'*32+'.txt',size=100,status='ready',chunks=[{'content':'Reference facts for simulation. '*10,'page_number':1}],embeddings=[[1.,0.]],embedding_model='fake'))
    publish(hosted,bot);c=hosted[3]
    conv=c.post('/api/public/bots/'+bot['id']+'/conversations',json={}).json()
    path='/api/public/conversations/'+conv['id']+'/messages';headers={'Authorization':'Bearer '+conv['token']}
    body={'message':'Explain the reference facts','request_id':'first'}
    first=c.post(path,headers=headers,json=body);assert first.status_code==200,first.text
    assert first.json()['sources']
    bot['config']['show_citations']=False
    bot=save(hosted,bot,instructor_notes='SECRET-INSTRUCTOR-NOT-FOR-MODEL')
    publish(hosted,bot)
    assert c.post(path,headers=headers,json=body).json()['sources']==[]
    current=c.post('/api/public/bots/'+bot['id']+'/conversations',json={}).json()
    answer=c.post('/api/public/conversations/'+current['id']+'/messages',headers={'Authorization':'Bearer '+current['token']},json={'message':'Explain reference facts','request_id':'second'})
    assert answer.status_code==200,answer.text
    assert answer.json()['sources']==[]
    assert 'SECRET-INSTRUCTOR' not in str(hosted[5])
    assert 'Do not mention source numbers' in hosted[5][-1][0]['content']


def test_retrieval_ceiling_deduplication_and_weak_matches(hosted):
    runtime=hosted[2];profile=BotConfig().model_dump()['profiles'][0]
    doc=SimpleNamespace(id='doc',filename='packet.txt',status='ready',knowledgebase='default',embedding_model='fake',chunks=[{'content':x,'page_number':i} for i,x in enumerate(['Relevant alpha','Relevant alpha','Relevant beta','Weak background','Unrelated'])],embeddings=[[1,0],[1,0],[.9,.1],[.4,.9],[-1,0]])
    results=runtime.retrieve([doc],profile,'Question',5)
    assert [r['content'] for r in results]==['Relevant alpha','Relevant beta']
    assert len(runtime.retrieve([doc],profile,'Question',1))==1
    assert BotConfig().top_k==3


def test_password_update_requires_owner_csrf_and_preserves_old_clients(hosted):
    bot=new_bot(hosted)
    body={'version':bot['version'],'config':bot['config'],'instructor_notes':'private','instructor_shared':True}
    c=hosted[4]('alice');assert c.put('/api/bots/'+bot['id'],json=body).status_code==422
    bot=save(hosted,bot,instructor_notes='private',instructor_shared=True,instructor_password='first-strong-password')
    bot=save(hosted,bot)  # Older API clients do not erase notes or passwords.
    assert bot['instructor_notes']=='private' and bot['instructor_shared']
    c=hosted[4]('bob');assert c.put('/api/bots/'+bot['id'],json={**body,'version':bot['version'],'instructor_password':'second-strong-password'}).status_code==404
    c=hosted[4]('alice');c.headers.pop('x-csrf-token')
    assert c.put('/api/bots/'+bot['id'],json={**body,'version':bot['version']}).status_code==403


def test_instructor_limits_are_separate_and_password_rotation_revokes(hosted):
    from asklit.hosted.models import RateBucket
    bot=new_bot(hosted)
    bot=save(hosted,bot,instructor_notes='Private',instructor_shared=True,instructor_password='first-strong-password')
    publish(hosted,bot)
    path='/api/public/bots/'+bot['id']+'/instructor-notes'
    bot=save(hosted,bot,instructor_password='second-strong-password')
    assert hosted[3].post(path,json={'password':'first-strong-password'}).status_code==403
    assert hosted[3].post(path,json={'password':'second-strong-password'}).status_code==200
    for _ in range(18):
        assert hosted[3].post(path,json={'password':'wrong'}).status_code==403
    limited=hosted[3].post(path,json={'password':'wrong'})
    assert limited.status_code==429 and 'instructor-password' in limited.text
    with hosted[1].transaction() as db:
        keys=list(db.scalars(select(RateBucket.key)))
    assert all(not k.startswith('login:') for k in keys)
    assert not any(k.startswith('instructor:user:') for k in keys)


def test_hidden_references_remain_available_to_authors_and_evaluation(hosted):
    config=BotConfig(show_citations=False).model_dump()
    doc=SimpleNamespace(id='doc',filename='packet.txt',status='ready',knowledgebase='default',embedding_model='fake',chunks=[{'content':'Reference facts. '*20,'page_number':1}],embeddings=[[1,0]])
    result=hosted[2].answer(config,[doc],'Facts?','default')
    assert result['sources'][0]['filename']=='packet.txt'
    doc.embeddings=[[-1,0]]
    assert len(hosted[2].retrieve([doc],config['profiles'][0],'And then?',3))==1

def test_malformed_presentation_import_is_rejected(hosted):
    import yaml
    bot=new_bot(hosted)
    c=hosted[3];path='/api/bots/'+bot['id']+'/workspace'
    workspace=yaml.safe_load(c.get(path).text)
    workspace['asklit_workspace']['app_config']['hosted_presentation']=['invalid']
    response=c.post(path,json={'version':bot['version'],'yaml':yaml.safe_dump(workspace)})
    assert response.status_code==422


def test_org_archived_during_answer_releases_turn(hosted):
    from asklit.hosted.models import Organization, Turn, Conversation
    bot=new_bot(hosted);publish(hosted,bot)
    c=hosted[3]
    conv=c.post('/api/public/bots/'+bot['id']+'/conversations',json={}).json()
    def archive_during_answer(messages,preset):
        with hosted[1].transaction() as db:
            db.get(Organization,'school').archived=True
        return 'Completed after archive'
    hosted[2].completion=archive_during_answer
    reply=c.post('/api/public/conversations/'+conv['id']+'/messages',headers={'Authorization':'Bearer '+conv['token']},json={'message':'hello','request_id':'archive'})
    assert reply.status_code==404
    with hosted[1].transaction() as db:
        assert db.get(Turn,(conv['id'],'archive')).status=='failed'
        assert db.get(Conversation,conv['id']).busy_until==0
