"""Invitation-only local signup; a single-use capability binds the school and role."""
import secrets
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import Invitation, Organization, LocalAccount, LoginSession
from .local_accounts import username, password_hash, new_local_user, reserve_login
from .security import audit, digest, token


class InviteToken(BaseModel):
    model_config=ConfigDict(extra='forbid')
    token: str=Field(min_length=20,max_length=200)


class InvitationSignup(InviteToken):
    email: str=Field(max_length=320)
    name: str=Field(min_length=1,max_length=200)
    password: str=Field(min_length=12,max_length=200)
    csrf: str=Field(min_length=1,max_length=200)

    @field_validator('email')
    @classmethod
    def clean_email(cls,value):return username(value)

    @field_validator('name')
    @classmethod
    def clean_name(cls,value):
        if not value.strip():raise ValueError('Enter your name.')
        return value.strip()


def valid_invitation(db, raw, lock=False):
    stmt=select(Invitation).where(Invitation.token_hash==digest(raw))
    if lock:stmt=stmt.with_for_update()
    invite=db.scalar(stmt)
    if not invite or invite.used or invite.expires<=time.time():
        raise HTTPException(404,'Invitation is invalid, expired, or already used.')
    return invite


def invitation_router(settings,store):
    router=APIRouter(prefix='/api/invitations')

    @router.post('/inspect')
    def inspect(body: InviteToken):
        with store.transaction() as db:
            invite=valid_invitation(db,body.token)
            org=db.get(Organization,invite.org_id)
            return {'organization':org.name,'org_id':org.id,'role':invite.role,'email':invite.recipient_email,'hosting_approved':invite.grants_hosting,'local_signup':bool(org.local_accounts_enabled and invite.grants_hosting and invite.recipient_email),'expires':invite.expires}

    @router.post('/register')
    def register(body: InvitationSignup,request: Request):
        expected=request.session.get('local_login_csrf','')
        if not expected or not secrets.compare_digest(body.csrf.encode(),expected.encode()):
            raise HTTPException(403,'Reload the invitation page and try again.')
        reserve_login(store,request,settings,'invitation:'+body.email)
        with store.transaction() as db:
            invite=valid_invitation(db,body.token)
            org=db.get(Organization,invite.org_id)
            if not org.local_accounts_enabled or not invite.grants_hosting or not invite.recipient_email:raise HTTPException(403,'Use an existing account or ask the LIT Lab for a named signup invitation.')
            if invite.recipient_email and body.email!=invite.recipient_email:raise HTTPException(403,'Use the email address this invitation was issued to.')
            if db.scalar(select(LocalAccount).where(LocalAccount.username==body.email)):
                raise HTTPException(409,'A local account with that email already exists. Sign in to accept the invitation.')
        hashed=password_hash(body.password)
        raw=token()
        try:
            with store.transaction() as db:
                invite=valid_invitation(db,body.token,True)
                org=db.get(Organization,invite.org_id)
                if not org.local_accounts_enabled:raise HTTPException(403,'Local signup is disabled for this organization.')
                # No role, organization, or approval fields come from the signup request.
                user=new_local_user(db,org.id,body.email,body.name,hashed,invite.role)
                account=db.get(LocalAccount,user.id);account.must_change_password=False
                user.author_approved=invite.grants_hosting
                invite.used=True
                db.add(LoginSession(token_hash=digest(raw),user_id=user.id,csrf=token(),expires=time.time()+86400))
                audit(db,user.id,org.id,'invitation.register',invite.token_hash)
                result={'org_id':org.id,'approved':user.author_approved}
        except IntegrityError:
            raise HTTPException(409,'A local account with that email already exists. Sign in instead.') from None
        request.session.clear()
        response=JSONResponse(result)
        response.set_cookie('asklit_session',raw,httponly=True,secure=settings.secure,samesite='lax',max_age=86400)
        return response
    return router
