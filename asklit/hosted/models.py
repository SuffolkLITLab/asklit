"""Hosted metadata schema, separate from each exported app's SQLite database."""

import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import event
from sqlalchemy.engine import make_url

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "hosted_users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(200))
    author_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class UsageEvent(Base):
    """One durable record per provider attempt; no prompts or answers."""
    __tablename__ = "hosted_usage_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("hosted_organizations.id"), index=True)
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("hosted_users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(40))
    preset: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="started")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Identity(Base):
    __tablename__ = "hosted_identities"
    issuer: Mapped[str] = mapped_column(String(500), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("hosted_users.id"))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    hosted_domain: Mapped[str] = mapped_column(String(255), default="")


class Organization(Base):
    __tablename__ = "hosted_organizations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str | None] = mapped_column(String(80), nullable=True)
    gallery_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    local_accounts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_voice_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_voice_daily_calls: Mapped[int] = mapped_column(Integer, default=100)
    allowed_domains: Mapped[list] = mapped_column(JSON, default=list)
    microsoft_tenants: Mapped[list] = mapped_column(JSON, default=list)
    quota_day: Mapped[str] = mapped_column(String(10), default="")
    calls: Mapped[int] = mapped_column(Integer, default=0)


class Membership(Base):
    __tablename__ = "hosted_memberships"
    org_id: Mapped[str] = mapped_column(
        ForeignKey("hosted_organizations.id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("hosted_users.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), default="member")


class LoginSession(Base):
    __tablename__ = "hosted_login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("hosted_users.id"))
    csrf: Mapped[str] = mapped_column(String(64))
    expires: Mapped[float] = mapped_column(Float)


class Invitation(Base):
    __tablename__ = "hosted_invitations"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("hosted_organizations.id"))
    role: Mapped[str] = mapped_column(String(20))
    expires: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Bot(Base):
    __tablename__ = "hosted_bots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(
        ForeignKey("hosted_organizations.id"), index=True
    )
    owner_id: Mapped[str] = mapped_column(ForeignKey("hosted_users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    draft: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    published_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    slug: Mapped[str | None] = mapped_column(String(80), nullable=True)
    access_mode: Mapped[str] = mapped_column(String(20), default="unlisted")
    access_version: Mapped[int] = mapped_column(Integer, default=1)


class OrganizationAlias(Base):
    __tablename__ = "hosted_organization_aliases"
    slug: Mapped[str] = mapped_column(String(80), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        ForeignKey("hosted_organizations.id"), index=True
    )


class BotAlias(Base):
    __tablename__ = "hosted_bot_aliases"
    org_id: Mapped[str] = mapped_column(
        ForeignKey("hosted_organizations.id"), primary_key=True
    )
    slug: Mapped[str] = mapped_column(String(80), primary_key=True)
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)


class PasswordKey(Base):
    __tablename__ = "hosted_password_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    lookup: Mapped[str] = mapped_column(String(64), index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class AccessGrant(Base):
    __tablename__ = "hosted_access_grants"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)
    access_version: Mapped[int] = mapped_column(Integer)
    password_id: Mapped[str | None] = mapped_column(
        ForeignKey("hosted_password_keys.id"), nullable=True
    )
    password_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("hosted_users.id"), nullable=True
    )
    session_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires: Mapped[float] = mapped_column(Float)


class Document(Base):
    __tablename__ = "hosted_documents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    knowledgebase: Mapped[str] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # Immutable after indexing: published revisions reference these exact chunks.
    chunks: Mapped[list] = mapped_column(JSON, default=list)
    embeddings: Mapped[list] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str] = mapped_column(String(200))


class Revision(Base):
    __tablename__ = "hosted_revisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)
    config: Mapped[dict] = mapped_column(JSON)
    document_ids: Mapped[list] = mapped_column(JSON)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Conversation(Base):
    __tablename__ = "hosted_conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    token_hash: Mapped[str] = mapped_column(String(64))
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("hosted_revisions.id"))
    profile: Mapped[str] = mapped_column(String(100))
    access_version: Mapped[int] = mapped_column(Integer, default=1)
    access_grant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    expires: Mapped[float] = mapped_column(Float)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    busy_until: Mapped[float] = mapped_column(Float, default=0)
    active_request: Mapped[str] = mapped_column(String(100), default="")


class Attachment(Base):
    __tablename__ = "hosted_attachments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("hosted_conversations.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(180))
    text: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = "hosted_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    bot_id: Mapped[str] = mapped_column(ForeignKey("hosted_bots.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[float] = mapped_column(Float, default=time.time)
    started: Mapped[float | None] = mapped_column(Float, nullable=True)
    heartbeat: Mapped[float | None] = mapped_column(Float, nullable=True)


class Turn(Base):
    __tablename__ = "hosted_turns"
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("hosted_conversations.id", ondelete="CASCADE"), primary_key=True
    )
    request_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class RateBucket(Base):
    __tablename__ = "hosted_rate_buckets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    expires: Mapped[float] = mapped_column(Float)


class Audit(Base):
    __tablename__ = "hosted_audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(100))
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Store:
    def __init__(self, url):
        # Supabase transaction pooling cannot retain per-session prepared statements.
        args = (
            {"prepare_threshold": None}
            if str(url).startswith("postgresql+psycopg:")
            else {}
        )
        sqlite = make_url(url).get_backend_name() == "sqlite"
        if sqlite:
            database = make_url(url).database
            if database and database != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            args = {"timeout": 30, "check_same_thread": False}
        self.engine = create_engine(url, pool_pre_ping=True, connect_args=args)
        if sqlite:

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, record):
                connection.isolation_level = None
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")

            @event.listens_for(self.engine, "begin")
            def begin_sqlite(connection):
                # SQLite ignores SELECT FOR UPDATE. Serialize short transactions
                # so API/worker claims, versions and budgets remain atomic locally.
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            yield session


class LocalAccount(Base):
    __tablename__ = "hosted_local_accounts"
    user_id: Mapped[str] = mapped_column(ForeignKey("hosted_users.id"), primary_key=True)
    username: Mapped[str] = mapped_column(String(320), unique=True)
    managed_org_id: Mapped[str] = mapped_column(ForeignKey("hosted_organizations.id"), index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
