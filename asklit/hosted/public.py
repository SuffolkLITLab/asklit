"""Public metadata is read-only. Conversation creation and inference require POST."""

import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .models import Bot, Conversation, RateBucket, Revision, Turn
from .runtime import profile_for, selected_documents
from .schemas import AccessRequest, Message, StartChat
from .access import (
    access_attempt,
    conversation_allowed,
    issue_access,
    public_path,
    require_access,
)
from .security import digest, reserve_calls, token


def public_bot(db, bot_id):
    bot = db.get(Bot, bot_id)
    if not bot or bot.archived or not bot.published_id:
        raise HTTPException(404, "This assistant is not published.")
    return bot


def limit_session_creation(db, request, settings):
    # Trust proxy headers only when uvicorn is explicitly configured for a trusted proxy.
    ip = request.client.host if request.client else "unknown"
    bucket = int(time.time() // 3600)
    key = hmac.new(
        settings.secret_key.encode(), f"{ip}:{bucket}".encode(), hashlib.sha256
    ).hexdigest()
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.execute(
        insert(RateBucket)
        .values(key=key, count=0, expires=(bucket + 2) * 3600)
        .on_conflict_do_nothing()
    )
    row = db.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
    if row.count >= 30:
        raise HTTPException(429, "Too many new conversations. Try again later.")
    row.count += 1


def public_router(settings, store, runtime):
    router = APIRouter(prefix="/api/public")

    def turn_error(status, code, message):
        return JSONResponse({"detail": message, "code": code}, status_code=status)

    def bot_metadata(db, bot, raw=""):
        locked = False
        try:
            require_access(db, bot, raw, settings)
        except HTTPException:
            locked = True
        common = {
            "id": bot.id,
            "access_mode": bot.access_mode,
            "locked": locked,
            "url": settings.base_url + public_path(db, bot),
            "retention_days": settings.retention_days,
        }
        if locked:
            return {
                **common,
                "title": "Protected AskLit",
                "welcome": "",
                "footer": "",
                "profiles": [],
            }
        config = db.get(Revision, bot.published_id).config
        return {
            **common,
            "title": config["title"],
            "welcome": config["welcome"],
            "footer": config["footer"],
            "profiles": [
                {
                    "key": p["key"],
                    "label": p["label"],
                    "conversation_starters": p["conversation_starters"],
                }
                for p in config["profiles"]
            ],
        }

    @router.get("/bots/{bot_id}")
    def metadata(bot_id: str, request: Request):
        with store.transaction() as db:
            bot = public_bot(db, bot_id)
            return bot_metadata(
                db,
                bot,
                request.headers.get("authorization", "").removeprefix("Bearer "),
            )

    @router.post("/bots/{bot_id}/access")
    def access(bot_id: str, body: AccessRequest, request: Request):
        with store.transaction() as db:
            bot = public_bot(db, bot_id)
            if bot.access_mode == "password" and not access_attempt(db, request, settings):
                raise HTTPException(429, "Too many access attempts. Try again later.")
            try:
                raw = issue_access(db, bot, request, body.password, settings)
            except HTTPException as exc:
                if bot.access_mode == "password" and exc.status_code == 403:
                    access_attempt(db, request, settings, failed=True)
                    return JSONResponse({"detail": exc.detail}, status_code=403)
                raise
            db.flush()
            return {"token": raw, "metadata": bot_metadata(db, bot, raw)}

    @router.post("/bots/{bot_id}/conversations")
    def start(bot_id: str, body: StartChat, request: Request):
        raw = token()
        with store.transaction() as db:
            bot = public_bot(db, bot_id)
            grant = require_access(db, bot, body.access_token, settings)
            revision = db.get(Revision, bot.published_id)
            profile_for(revision.config, body.profile)
            limit_session_creation(db, request, settings)
            conversation = Conversation(
                bot_id=bot.id,
                revision_id=revision.id,
                profile=body.profile,
                access_version=bot.access_version,
                access_grant=grant.token_hash if grant else None,
                token_hash=digest(raw),
                expires=time.time() + settings.retention_days * 86400,
            )
            db.add(conversation)
            db.flush()
            return {"id": conversation.id, "token": raw, "revision_id": revision.id}

    def authorized_conversation(db, conversation_id, request, require_published=True):
        conversation = db.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
        bearer = request.headers.get("authorization", "")
        raw = bearer[7:] if bearer.startswith("Bearer ") else ""
        if (
            not conversation
            or not raw
            or not secrets.compare_digest(conversation.token_hash, digest(raw))
            or conversation.expires <= time.time()
        ):
            raise HTTPException(404, "Conversation not found or expired.")
        if require_published:
            bot = public_bot(db, conversation.bot_id)
            if not conversation_allowed(db, bot, conversation, settings):
                raise HTTPException(
                    403, "Access has changed. Unlock this AskLit again."
                )
        return conversation

    @router.delete("/conversations/{conversation_id}")
    def forget(conversation_id: str, request: Request):
        with store.transaction() as db:
            conversation = authorized_conversation(
                db, conversation_id, request, require_published=False
            )
            if conversation.busy_until > time.time():
                raise HTTPException(
                    409,
                    "Wait for the current answer before clearing this conversation.",
                )
            from sqlalchemy import delete

            db.execute(delete(Turn).where(Turn.conversation_id == conversation_id))
            db.delete(conversation)
        return {"deleted": True}

    @router.post("/conversations/{conversation_id}/messages")
    def message(conversation_id: str, body: Message, request: Request):
        with store.transaction() as db:
            conversation = authorized_conversation(db, conversation_id, request)
            previous = db.get(Turn, (conversation.id, body.request_id))
            if previous:
                if previous.input_hash != digest(body.message):
                    return turn_error(
                        409,
                        "request_conflict",
                        "This request ID belongs to a different message.",
                    )
                if previous.status == "succeeded":
                    return previous.result
                if (
                    previous.status == "failed"
                    or conversation.busy_until <= time.time()
                    or conversation.active_request != body.request_id
                ):
                    previous.status = "failed"
                    return turn_error(
                        410,
                        "turn_failed",
                        "The previous attempt failed or timed out. Send again to start a new attempt.",
                    )
                return turn_error(
                    409,
                    "turn_pending",
                    "This answer is still in progress. Try again shortly to retrieve it.",
                )
            if conversation.busy_until > time.time():
                return turn_error(
                    409, "conversation_busy", "An answer is already in progress."
                )
            if conversation.turns >= 12:
                raise HTTPException(
                    429, "This conversation has reached its 12-question limit."
                )
            bot = public_bot(db, conversation.bot_id)
            reserve_calls(db, bot.org_id, settings, audience="public")
            conversation.turns += 1
            conversation.busy_until = time.time() + 180
            conversation.active_request = body.request_id
            db.add(
                Turn(
                    conversation_id=conversation.id,
                    request_id=body.request_id,
                    input_hash=digest(body.message),
                )
            )
            revision = db.get(Revision, conversation.revision_id)
            config = revision.config
            docs = selected_documents(db, bot.id, revision.document_ids)
            history = conversation.messages
            profile = conversation.profile
        try:
            result = runtime.answer(config, docs, body.message, profile, history)
        except Exception:
            with store.transaction() as db:
                conv = db.scalar(
                    select(Conversation)
                    .where(Conversation.id == conversation_id)
                    .with_for_update()
                )
                turn = db.get(Turn, (conversation_id, body.request_id))
                if turn:
                    turn.status = "failed"
                if conv and conv.active_request == body.request_id:
                    conv.busy_until = 0
            raise HTTPException(
                502,
                "The assistant could not answer. Please try again with a new request.",
            ) from None
        with store.transaction() as db:
            conv = db.scalar(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .with_for_update()
            )
            turn = db.get(Turn, (conversation_id, body.request_id))
            if not conv or not turn:
                return turn_error(
                    410, "turn_failed", "This conversation has been cleared."
                )
            current_bot = db.get(Bot, conv.bot_id)
            active = (
                current_bot
                and not current_bot.archived
                and current_bot.published_id
                and conversation_allowed(db, current_bot, conv, settings)
            )
            owns_lease = conv.active_request == body.request_id
            if (
                not active
                or conv.expires <= time.time()
                or conv.busy_until <= time.time()
                or not owns_lease
            ):
                turn.status = "failed"
                if owns_lease:
                    conv.busy_until = 0
                # Returning instead of raising commits the terminal status and releases
                # the lease even if the assistant was unpublished during inference.
                return turn_error(
                    410 if active else 404,
                    "turn_failed",
                    "The answer expired or the assistant is no longer published. Start a new attempt.",
                )
            conv.messages = history + [
                {"role": "user", "content": body.message},
                {"role": "assistant", "content": result["answer"]},
            ]
            conv.busy_until = 0
            turn.status, turn.result = "succeeded", result
            return result

    return router
