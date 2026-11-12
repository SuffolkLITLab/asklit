"""Operator-only organization closure and explicit irreversible erasure."""
import time
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, update
from .models import (Organization, Bot, Job, Conversation, UsageEvent, LocalAccount,
    User, LoginSession, Membership, Invitation, OrganizationAlias, BotAlias,
    AccessGrant, PasswordKey, Attachment, Turn, Revision, Document, Audit,
    DeletedOrganizationSpend, RateBucket, PendingObjectDeletion, Identity)
from .usage import platform_operator
from .security import audit


class ArchiveOrganization(BaseModel):
    model_config = ConfigDict(extra='forbid')
    archived: bool


class DeleteOrganization(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmation: str = Field(min_length=1, max_length=200)


def lifecycle_router(settings, store):
    router = APIRouter(prefix='/api/operator/organizations')

    @router.patch('/{org_id}')
    def archive(org_id: str, body: ArchiveOrganization, request: Request):
        with store.transaction() as db:
            actor = platform_operator(db, request, True)
            org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
            if not org: raise HTTPException(404, 'Organization not found.')
            if org.archived != body.archived:
                org.archived = body.archived
                if body.archived:
                    bots = select(Bot.id).where(Bot.org_id == org_id)
                    db.execute(update(Bot).where(Bot.org_id == org_id).values(access_version=Bot.access_version+1))
                    db.execute(update(Job).where(Job.bot_id.in_(bots), Job.status == 'queued').values(status='failed', result={'error': 'Organization archived. Run this task again after restoring it.'}))
                audit(db, actor.id, org_id, 'organization.archive' if body.archived else 'organization.restore', org_id)
            return {'id': org.id, 'archived': org.archived}

    @router.delete('/{org_id}')
    def erase(org_id: str, body: DeleteOrganization, request: Request):
        with store.transaction() as db:
            actor = platform_operator(db, request, True)
            org = db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
            if not org: raise HTTPException(404, 'Organization not found.')
            if not org.archived:raise HTTPException(409, 'Archive the organization before deleting it.')
            if ' '.join(body.confirmation.split()) != ' '.join(org.name.split()):
                raise HTTPException(409, 'Enter the exact name of the organization to confirm deletion.')
            local_users = select(LocalAccount.user_id).where(LocalAccount.managed_org_id == org_id)
            if db.scalar(select(User.id).where(User.id.in_(local_users), User.platform_admin.is_(True)).limit(1)):
                raise HTTPException(409, 'This organization manages an Admin login. Move that login to another organization before deleting it.')
            local_ids = list(db.scalars(local_users))
            if (db.scalar(select(Membership.user_id).where(Membership.user_id.in_(local_ids),Membership.org_id != org_id).limit(1))
                or db.scalar(select(Bot.id).where(Bot.owner_id.in_(local_ids),Bot.org_id != org_id).limit(1))
                or db.scalar(select(UsageEvent.id).where(UsageEvent.owner_id.in_(local_ids),UsageEvent.org_id != org_id).limit(1))):
                raise HTTPException(409, 'A local login managed here is used by another organization. Move its management before deleting this organization.')
            bots = select(Bot.id).where(Bot.org_id == org_id)
            conversations = select(Conversation.id).where(Conversation.bot_id.in_(bots))
            if (db.scalar(select(Job.id).where(Job.bot_id.in_(bots), Job.status == 'running').limit(1))
                or db.scalar(select(Conversation.id).where(Conversation.bot_id.in_(bots), Conversation.busy_until > time.time()).limit(1))
                or db.scalar(select(UsageEvent.id).where(UsageEvent.org_id == org_id, UsageEvent.status == 'started', UsageEvent.created > time.time()-900).limit(1))):
                raise HTTPException(409, 'Work is still finishing. The organization is archived; retry deletion in a few minutes.')
            # Match the spending reservation lock before transferring totals. No school,
            # author, bot, prompt, or per-request history survives in these totals.
            from .budget import month_start
            key = 'monthly-ai-budget:' + month_start().strftime('%Y-%m')
            if db.bind.dialect.name == 'postgresql':
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            db.execute(insert(RateBucket).values(key=key,count=0,expires=time.time()+70*86400).on_conflict_do_nothing())
            db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
            totals = {}
            for event in db.scalars(select(UsageEvent).where(UsageEvent.org_id == org_id)):
                month = datetime.fromtimestamp(event.created, timezone.utc).strftime('%Y-%m')
                amount = event.estimated_cost_usd if event.estimated_cost_usd is not None else event.budget_reserved_usd
                totals[month] = totals.get(month, 0) + (amount if amount is not None else 1.0)
            for month, amount in sorted(totals.items()):
                db.execute(insert(DeletedOrganizationSpend).values(month=month,amount=amount).on_conflict_do_update(index_elements=['month'],set_={'amount':DeletedOrganizationSpend.amount+amount}))
            keys = set(db.scalars(select(Document.storage_key).where(Document.bot_id.in_(bots))))
            for job in db.scalars(select(Job).where(Job.bot_id.in_(bots))):
                if job.result.get('storage_key'): keys.add(job.result['storage_key'])
            # Queue cleanup atomically with erasure. The worker deletes private
            # objects outside database locks and retries after storage outages.
            for key in keys:
                db.execute(insert(PendingObjectDeletion).values(key=key).on_conflict_do_nothing())
            for model, condition in [
                (Attachment, Attachment.conversation_id.in_(conversations)),
                (Turn, Turn.conversation_id.in_(conversations)),
                (Conversation, Conversation.bot_id.in_(bots)),
                (AccessGrant, AccessGrant.bot_id.in_(bots) | AccessGrant.user_id.in_(local_ids)),
                (PasswordKey, PasswordKey.bot_id.in_(bots)),
                (BotAlias, BotAlias.org_id == org_id),
                (UsageEvent, UsageEvent.org_id == org_id),
                (Job, Job.bot_id.in_(bots)), (Revision, Revision.bot_id.in_(bots)),
                (Document, Document.bot_id.in_(bots)),
                (LoginSession, LoginSession.user_id.in_(local_users)),
                (LocalAccount, LocalAccount.managed_org_id == org_id),
                (Membership, Membership.org_id == org_id),
                (Invitation, Invitation.org_id == org_id),
                (OrganizationAlias, OrganizationAlias.org_id == org_id),
                (Audit, Audit.org_id == org_id), (Bot, Bot.org_id == org_id),
            ]: db.execute(delete(model).where(condition).execution_options(synchronize_session=False))
            db.delete(org)
            for user_id in local_ids:
                if not db.scalar(select(Identity.user_id).where(Identity.user_id == user_id).limit(1)):
                    db.execute(delete(User).where(User.id == user_id))
            audit(db, actor.id, 'service', 'organization.delete', org_id)
        return {'deleted': True}

    return router
