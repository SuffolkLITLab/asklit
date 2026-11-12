"""Operator approval and aggregate usage reports, without conversation content."""
import time
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, func, case, update, or_
from typing import Literal
from .models import User, Bot, UsageEvent, Organization, Audit
from .security import login_session, membership

class Approval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved: bool


def platform_operator(db, request, mutation=False):
    session = login_session(db, request, mutation)
    user = db.get(User, session.user_id)
    if not user.platform_admin:
        raise HTTPException(403, 'Only a LIT Lab service operator can manage hosting approvals.')
    return user


def usage_report(db, days, org_id=None):
    since = time.time() - days * 86400
    stmt = select(
        UsageEvent.org_id, Organization.name, UsageEvent.bot_id, Bot.title,
        UsageEvent.preset, UsageEvent.model, UsageEvent.purpose, UsageEvent.owner_id,
        func.count().label('requests'),
        func.sum(case((UsageEvent.status == 'succeeded', 1), else_=0)).label('succeeded'),
        func.sum(case((UsageEvent.status == 'failed', 1), else_=0)).label('failed'),
        func.sum(case((UsageEvent.status == 'started', 1), else_=0)).label('unfinished'),
        func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label('input_tokens'),
        func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label('output_tokens'),
        func.sum(func.coalesce(UsageEvent.estimated_cost_usd, 0)).label('estimated_cost_usd'),
        func.sum(case((UsageEvent.estimated_cost_usd.is_(None), 1), else_=0)).label('unknown_cost_requests'),
    ).join(Organization,Organization.id==UsageEvent.org_id).join(Bot,Bot.id==UsageEvent.bot_id).where(UsageEvent.created>=since)
    if org_id: stmt=stmt.where(UsageEvent.org_id==org_id)
    stmt=stmt.group_by(UsageEvent.org_id, Organization.name, UsageEvent.bot_id, Bot.title, UsageEvent.preset, UsageEvent.model, UsageEvent.purpose, UsageEvent.owner_id)
    rows=[]
    for row in db.execute(stmt):
        item=dict(row._mapping);item['organization']=item.pop('name');item['asklit']=item.pop('title');rows.append(item)
    keys=['requests','succeeded','failed','unfinished','input_tokens','output_tokens','estimated_cost_usd','unknown_cost_requests']
    return {'days':days,'since':since,'totals':{k:sum(r[k] for r in rows) for k in keys},'rows':rows,
            'note':'Estimated model cost in USD, before tax and hosting fees. Unknown-cost calls are not included in the estimate. Reconcile with AWS billing; failures or interrupted calls may still be charged.'}


def usage_router(settings, store):
    router=APIRouter(prefix='/api')

    @router.get('/operator/accounts')
    def accounts(request: Request, page: int=Query(1,ge=1), q: str=Query("",max_length=320), status: Literal["all", "pending", "approved", "deleted"]="all"):
        with store.transaction() as db:
            platform_operator(db,request)
            stmt = select(User).where(User.approval_deleted.is_(status == "deleted"))
            if status == "pending": stmt = stmt.where(User.author_approved.is_(False))
            if status == "approved": stmt = stmt.where(User.author_approved.is_(True))
            if q.strip():
                term = q.strip().lower()
                stmt = stmt.where(or_(func.lower(User.name).contains(term, autoescape=True), func.lower(User.email).contains(term, autoescape=True), func.lower(User.id).contains(term, autoescape=True)))
            users=db.scalars(stmt.order_by(User.author_approved,User.id).offset((page-1)*100).limit(101)).all()
            return {'has_more':len(users)>100,'page':page,'accounts':[{'id':u.id,'name':u.name,'email':u.email,'approved':u.author_approved,'operator':u.platform_admin,'request_deleted':u.approval_deleted} for u in users[:100]]}

    @router.patch('/operator/accounts/{user_id}')
    def approve(user_id: str, body: Approval, request: Request):
        with store.transaction() as db:
            actor=platform_operator(db,request,True)
            user=db.scalar(select(User).where(User.id==user_id).with_for_update())
            if not user:raise HTTPException(404,'Account not found.')
            if user.platform_admin and not body.approved:raise HTTPException(409,'Operator accounts cannot be suspended here.')
            user.author_approved=body.approved
            user.approval_deleted=False
            if not body.approved:
                db.execute(update(Bot).where(Bot.owner_id==user.id).values(published_id=None,access_version=Bot.access_version+1))
            # Operator audit belongs to each affected workspace, avoiding a fabricated org.
            from .models import Membership
            orgs=db.scalars(select(Membership.org_id).where(Membership.user_id==user.id)).all()
            for org in orgs:db.add(Audit(user_id=actor.id,org_id=org,action='hosting.approve' if body.approved else 'hosting.suspend',target=user.id))
            return {'id':user.id,'approved':user.author_approved}


    def change_request(user_id, request, deleted):
        with store.transaction() as db:
            actor = platform_operator(db, request, True)
            user = db.scalar(select(User).where(User.id == user_id).with_for_update())
            if not user: raise HTTPException(404, 'Account not found.')
            if user.platform_admin or user.author_approved:
                raise HTTPException(409, 'Only pending approval requests can be deleted or restored.')
            user.approval_deleted = deleted
            from .models import Membership
            for org_id in db.scalars(select(Membership.org_id).where(Membership.user_id == user_id)):
                db.add(Audit(user_id=actor.id, org_id=org_id, action='hosting.request_delete' if deleted else 'hosting.request_restore', target=user.id))
            return {'id': user.id, 'request_deleted': deleted}

    @router.delete('/operator/accounts/{user_id}/approval-request')
    def delete_request(user_id: str, request: Request):
        return change_request(user_id, request, True)

    @router.post('/operator/accounts/{user_id}/approval-request')
    def restore_request(user_id: str, request: Request):
        return change_request(user_id, request, False)

    @router.get('/operator/usage')
    def all_usage(request: Request, days: int=Query(30,ge=1,le=366)):
        with store.transaction() as db:
            platform_operator(db,request)
            report = usage_report(db,days)
            from .budget import budget_report
            report["budget"] = budget_report(db, settings)
            return report

    @router.get('/organizations/{org_id}/usage')
    def organization_usage(org_id: str, request: Request, days: int=Query(30,ge=1,le=366)):
        with store.transaction() as db:
            session=login_session(db,request)
            membership(db,org_id,session.user_id,True)
            return usage_report(db,days,org_id)
    return router

# ContextVars isolate concurrent FastAPI threads and evaluation calls.
from contextvars import ContextVar
from contextlib import contextmanager
_usage_context = ContextVar('asklit_usage_context', default=None)

@contextmanager
def meter(store, bot, purpose):
    marker=_usage_context.set((store,bot.org_id,bot.id,purpose))
    try:yield
    finally:_usage_context.reset(marker)


def begin_call(settings, preset, model, messages=None):
    context=_usage_context.get()
    if context is None:
        if settings.require_author_approval or settings.monthly_ai_budget_usd is not None:raise RuntimeError('Model call requires a usage context.')
        return None
    store,org_id,bot_id,purpose=context
    with store.transaction() as db:
        org=db.scalar(select(Organization).where(Organization.id==org_id).with_for_update())
        if not org or org.archived:raise HTTPException(403,"This organization is archived.")
        bot=db.get(Bot,bot_id)
        owner=db.get(User,bot.owner_id) if bot else None
        if settings.require_author_approval and (not owner or not owner.author_approved):
            raise HTTPException(403,'The author must be approved for hosting before using AI services.')
        from .budget import reserve_budget
        reservation = reserve_budget(db, settings, model, messages)
        event=UsageEvent(budget_reserved_usd=reservation,org_id=org_id,bot_id=bot_id,owner_id=bot.owner_id,purpose=purpose,preset=preset,model=model['model'],
                         input_rate=model.get('input_usd_per_million'),output_rate=model.get('output_usd_per_million'))
        db.add(event);db.flush();event_id=event.id
    return store,event_id


def finish_call(handle, status, usage, elapsed):
    if handle is None:return
    store,event_id=handle
    with store.transaction() as db:
        event=db.get(UsageEvent,event_id)
        if event is None:return  # Deleted organization: reservation remains in anonymous totals.
        event.status=status;event.latency_ms=int(elapsed*1000)
        if usage:
            event.input_tokens=usage.get('input_tokens')
            event.output_tokens=usage.get('output_tokens')
            # No prompt caching is requested by this service. If a provider reports
            # cached usage, keep cost unknown rather than applying incorrect rates.
            if not usage.get('cached_tokens') and not usage.get('unpriced_context') and all(v is not None for v in [event.input_tokens,event.output_tokens,event.input_rate,event.output_rate]):
                event.estimated_cost_usd=(event.input_tokens*event.input_rate+event.output_tokens*event.output_rate)/1_000_000
