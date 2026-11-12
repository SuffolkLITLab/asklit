"""Service operators provision school organizations and named invitations."""
import re
import time
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from .models import Organization, OrganizationAlias, Membership, User, Invitation
from .local_accounts import username
from .security import audit, digest, token
from .usage import platform_operator, usage_report


class NewOrganization(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=80, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        if not value.strip(): raise ValueError('Enter an organization name.')
        return value.strip()


class SchoolInvite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=320)
    role: Literal['member', 'admin', 'owner'] = 'member'

    @field_validator('email')
    @classmethod
    def clean_email(cls, value): return username(value)


def admin_organizations_router(settings, store):
    router=APIRouter(prefix='/api/operator')

    @router.get('/organizations')
    def organizations(request: Request, days: int=Query(30,ge=1,le=366), q: str=Query('',max_length=200), page: int=Query(1,ge=1)):
        with store.transaction() as db:
            platform_operator(db,request)
            stmt=select(Organization)
            if q.strip():
                term=q.strip().lower()
                stmt=stmt.where(or_(func.lower(Organization.name).contains(term,autoescape=True),func.lower(Organization.slug).contains(term,autoescape=True)))
            orgs=db.scalars(stmt.order_by(Organization.name,Organization.id).offset((page-1)*100).limit(101)).all()
            ids=[o.id for o in orgs[:100]]
            counts={r.org_id:(r.members,r.approved) for r in db.execute(select(Membership.org_id,func.count().label('members'),func.count().filter(User.author_approved.is_(True)).label('approved')).join(User,User.id==Membership.user_id).where(Membership.org_id.in_(ids)).group_by(Membership.org_id))}
            # Aggregate the existing content-free usage rows once, across schools.
            usage={oid:{'requests':0,'estimated_cost_usd':0,'unknown_cost_requests':0} for oid in ids}
            for row in usage_report(db,days)['rows']:
                if row['org_id'] in usage:
                    for key in usage[row['org_id']]:usage[row['org_id']][key]+=row[key]
            return {'page':page,'has_more':len(orgs)>100,'days':days,'organizations':[{'id':o.id,'name':o.name,'slug':o.slug,'members':counts.get(o.id,(0,0))[0],'approved_members':counts.get(o.id,(0,0))[1],**usage[o.id]} for o in orgs[:100]]}

    @router.post('/organizations')
    def create(body: NewOrganization, request: Request):
        if body.slug in {'api','auth','static','gallery','health','docs','redoc','openapi','o','a','privacy','terms'} or re.fullmatch(r'org-[a-f0-9]{32}',body.slug):
            raise HTTPException(422,'That organization prefix is reserved.')
        try:
            with store.transaction() as db:
                operator=platform_operator(db,request,True)
                if db.get(OrganizationAlias,body.slug):raise HTTPException(409,'That organization prefix is already in use.')
                org=Organization(name=body.name,slug=body.slug,local_accounts_enabled=True)
                db.add(org);db.flush()
                db.add(OrganizationAlias(slug=body.slug,org_id=org.id))
                audit(db,operator.id,org.id,'organization.provision',org.id)
                return {'id':org.id,'name':org.name,'slug':org.slug}
        except IntegrityError:raise HTTPException(409,'That organization prefix is already in use.') from None

    @router.get('/organizations/{org_id}/usage')
    def usage(org_id: str, request: Request, days: int=Query(30,ge=1,le=366)):
        with store.transaction() as db:
            platform_operator(db,request)
            if not db.get(Organization,org_id):raise HTTPException(404,'Organization not found.')
            return usage_report(db,days,org_id)

    @router.get('/organizations/{org_id}/members')
    def members(org_id: str, request: Request):
        with store.transaction() as db:
            platform_operator(db,request)
            if not db.get(Organization,org_id):raise HTTPException(404,'Organization not found.')
            return [{'id':u.id,'name':u.name,'email':u.email,'role':m.role,'approved':u.author_approved} for u,m in db.execute(select(User,Membership).join(Membership,Membership.user_id==User.id).where(Membership.org_id==org_id).order_by(User.name))]

    @router.post('/organizations/{org_id}/invitations')
    def invite(org_id: str, body: SchoolInvite, request: Request):
        raw=token()
        with store.transaction() as db:
            operator=platform_operator(db,request,True)
            org=db.get(Organization,org_id)
            if not org:raise HTTPException(404,'Organization not found.')
            existing=db.scalar(select(User.id).join(Membership, Membership.user_id==User.id).where(Membership.org_id==org.id,func.lower(User.email)==body.email).limit(1))
            if existing:raise HTTPException(409,'This person is already a member. Use Hosting approvals for approval, or organization member settings to change their role.')
            invitation=Invitation(token_hash=digest(raw),org_id=org.id,role=body.role,recipient_email=body.email,grants_hosting=True,expires=time.time()+7*86400)
            db.add(invitation)
            audit(db,operator.id,org.id,'invitation.provision',invitation.token_hash)
            return {'url':f'{settings.base_url}/#invite={raw}','expires':invitation.expires,'email':body.email,'id':invitation.token_hash}

    @router.get('/organizations/{org_id}/invitations')
    def invitations(org_id: str,request: Request):
        with store.transaction() as db:
            platform_operator(db,request)
            return [{'id':i.token_hash,'email':i.recipient_email,'role':i.role,'expires':i.expires,'hosting_approved':i.grants_hosting} for i in db.scalars(select(Invitation).where(Invitation.org_id==org_id,Invitation.used.is_(False),Invitation.expires>time.time()).order_by(Invitation.expires))]

    @router.delete('/organizations/{org_id}/invitations/{invitation_id}')
    def revoke(org_id: str,invitation_id: str,request: Request):
        with store.transaction() as db:
            operator=platform_operator(db,request,True)
            invite=db.scalar(select(Invitation).where(Invitation.token_hash==invitation_id,Invitation.org_id==org_id).with_for_update())
            if not invite:raise HTTPException(404,'Invitation not found.')
            invite.used=True
            audit(db,operator.id,org_id,'invitation.revoke',invitation_id)
            return {'revoked':True}
    return router
