"""Authenticated organization and authoring API."""

import copy
import hashlib
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from .storage import object_store
from .access import ensure_links, public_path
from sqlalchemy import func, or_, select
from asklit.experiments import parse_scenario_csv, scenarios_to_csv
from .evaluation import plan, generate, scenario_from_scaffold, scaffold_scenarios

from .models import (
    Audit,
    Bot,
    Document,
    Invitation,
    Identity,
    Job,
    Membership,
    Organization,
    Revision,
    User,
    uid,
)
from .runtime import MAX_CHUNKS, profile_for, selected_documents, validate_config
from .schemas import (
    GenerateScenarios,
    ScenarioCSV,
    AcceptInvite,
    BotConfig,
    ImportWorkspace,
    Invite,
    Name,
    NewBot,
    Preview,
    RenameKnowledgebase,
    Role,
    SaveBot,
    Transfer,
    Version,
)
from .security import (
    accessible_bot,
    audit,
    digest,
    login_session,
    membership,
    reserve_calls,
    token,
)


def bot_view(bot):
    return {
        "id": bot.id,
        "org_id": bot.org_id,
        "owner_id": bot.owner_id,
        "title": bot.title,
        "version": bot.version,
        "published_id": bot.published_id,
        "config": bot.draft,
    }


def check_version(bot, version):
    if bot.version != version:
        raise HTTPException(
            409, "This draft changed in another tab. Reload before saving."
        )


def ready_documents(db, bot):
    docs = selected_documents(db, bot.id)
    if any(d.status != "ready" for d in docs):
        raise HTTPException(
            409, "Wait for indexing to finish, or remove failed documents."
        )
    if sum(len(d.chunks) for d in docs) > MAX_CHUNKS:
        raise HTTPException(422, f"A bot can contain at most {MAX_CHUNKS} chunks.")
    if len({d.embedding_model for d in docs}) > 1:
        raise HTTPException(
            422,
            "All sources must use the same embedding model. Reupload sources into a new bot.",
        )
    for p in bot.draft["profiles"]:
        available = {d.filename for d in docs if d.knowledgebase == p["knowledgebase"]}
        if set(p["connected_files"]) - available:
            raise HTTPException(
                422, f"Some connected files for {p['label']} are missing."
            )
    return docs


def enqueue(db, bot, kind, payload):
    existing = db.scalar(
        select(Job.id).where(
            Job.bot_id == bot.id,
            Job.status.in_(["queued", "running"]),
            Job.kind.in_(["evaluate", "export"]),
        )
    )
    if existing and kind != "index":
        raise HTTPException(
            409, "A task is already running for this bot. Wait for it to finish."
        )
    job = Job(bot_id=bot.id, kind=kind, payload=payload)
    db.add(job)
    db.flush()
    return {"id": job.id, "status": job.status}


def management_router(settings, store, runtime):
    router = APIRouter(prefix="/api")

    @router.post("/organizations")
    def create_org(body: Name, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            org = Organization(name=body.name)
            db.add(org)
            db.flush()
            db.add(Membership(org_id=org.id, user_id=session.user_id, role="owner"))
            audit(db, session.user_id, org.id, "organization.create", org.id)
            return {"id": org.id, "name": org.name}

    @router.get("/organizations/{org_id}/members")
    def members(org_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            membership(db, org_id, session.user_id, True)
            rows = db.execute(
                select(User, Membership)
                .join(Membership)
                .where(Membership.org_id == org_id)
            ).all()
            return [
                {
                    "id": u.id,
                    "name": u.name,
                    "email": u.email,
                    "role": m.role,
                    "identities": [
                        {"issuer": i.issuer, "email_verified": i.email_verified}
                        for i in db.scalars(
                            select(Identity).where(Identity.user_id == u.id)
                        )
                    ],
                }
                for u, m in rows
            ]

    @router.post("/organizations/{org_id}/invitations")
    def invite(org_id: str, body: Invite, request: Request):
        raw = token()
        with store.transaction() as db:
            session = login_session(db, request, True)
            member = membership(db, org_id, session.user_id, True)
            if body.role == "admin" and member.role != "owner":
                raise HTTPException(403, "Only an owner can invite administrators.")
            db.add(
                Invitation(
                    token_hash=digest(raw),
                    org_id=org_id,
                    role=body.role,
                    expires=time.time() + 7 * 86400,
                )
            )
            audit(db, session.user_id, org_id, "invitation.create", body.role)
        # A fragment keeps the invitation capability out of HTTP request/access logs.
        return {"url": f"{settings.base_url}/#invite={raw}"}

    @router.post("/invitations/accept")
    def accept_invite(body: AcceptInvite, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            invite = db.scalar(
                select(Invitation)
                .where(Invitation.token_hash == digest(body.token))
                .with_for_update()
            )
            if not invite or invite.used or invite.expires <= time.time():
                raise HTTPException(404, "Invitation is invalid or expired.")
            member = db.get(Membership, (invite.org_id, session.user_id))
            if not member:
                db.add(
                    Membership(
                        org_id=invite.org_id, user_id=session.user_id, role=invite.role
                    )
                )
            invite.used = True
            audit(
                db, session.user_id, invite.org_id, "invitation.accept", session.user_id
            )
            return {"org_id": invite.org_id}

    @router.patch("/organizations/{org_id}/members/{user_id}")
    def change_role(org_id: str, user_id: str, body: Role, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            # Serialize all membership edits, including last-owner checks.
            db.scalar(
                select(Organization).where(Organization.id == org_id).with_for_update()
            )
            caller = membership(db, org_id, session.user_id, True)
            if caller.role != "owner":
                raise HTTPException(403, "Only organization owners can change roles.")
            target = membership(db, org_id, user_id)
            owners = db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.org_id == org_id, Membership.role == "owner")
            )
            if target.role == "owner" and body.role != "owner" and owners <= 1:
                raise HTTPException(409, "An organization must retain an owner.")
            target.role = body.role
            audit(db, session.user_id, org_id, "membership.role", user_id)
            return {"role": target.role}

    @router.delete("/organizations/{org_id}/members/{user_id}")
    def remove_member(org_id: str, user_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            db.scalar(
                select(Organization).where(Organization.id == org_id).with_for_update()
            )
            caller = membership(db, org_id, session.user_id, True)
            target = membership(db, org_id, user_id)
            if caller.role != "owner" or target.role == "owner":
                raise HTTPException(
                    403, "An owner must demote an owner before removing them."
                )
            db.delete(target)
            audit(db, session.user_id, org_id, "membership.remove", user_id)
            return {"removed": True}

    @router.get("/organizations/{org_id}/audit")
    def events(org_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            membership(db, org_id, session.user_id, True)
            events = db.scalars(
                select(Audit)
                .where(Audit.org_id == org_id)
                .order_by(Audit.created.desc())
                .limit(100)
            )
            return [
                {
                    "action": e.action,
                    "user_id": e.user_id,
                    "target": e.target,
                    "created": e.created,
                }
                for e in events
            ]

    @router.get("/bots")
    def bots(request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            rows = db.scalars(
                select(Bot)
                .join(Membership, Membership.org_id == Bot.org_id)
                .where(
                    Membership.user_id == session.user_id,
                    Bot.archived.is_(False),
                    or_(
                        Bot.owner_id == session.user_id,
                        Membership.role.in_(["owner", "admin"]),
                    ),
                )
                .order_by(Bot.title)
            )
            return [bot_view(b) for b in rows]

    @router.post("/bots")
    def new_bot(body: NewBot, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            membership(db, body.org_id, session.user_id)
            preset = next(iter(settings.models))
            bot = Bot(
                org_id=body.org_id,
                owner_id=session.user_id,
                title=body.title,
                draft=BotConfig(title=body.title, model_preset=preset).model_dump(),
            )
            db.add(bot)
            db.flush()
            ensure_links(db, bot)
            audit(db, session.user_id, bot.org_id, "bot.create", bot.id)
            return bot_view(bot)

    @router.get("/bots/{bot_id}")
    def get_bot(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            return bot_view(accessible_bot(db, bot_id, session.user_id))

    @router.put("/bots/{bot_id}")
    def save_bot(bot_id: str, body: SaveBot, request: Request):
        config = validate_config(body.config.model_dump(), settings)
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            check_version(bot, body.version)
            bot.draft, bot.title, bot.version = config, config["title"], bot.version + 1
            audit(db, session.user_id, bot.org_id, "bot.save", bot.id)
            return bot_view(bot)

    @router.delete("/bots/{bot_id}")
    def archive_bot(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            bot.archived, bot.published_id = True, None
            audit(db, session.user_id, bot.org_id, "bot.archive", bot.id)
            return {"archived": True}

    @router.post("/bots/{bot_id}/owner")
    def transfer_bot(bot_id: str, body: Transfer, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            membership(db, bot.org_id, session.user_id, True)
            membership(db, bot.org_id, body.owner_id)
            bot.owner_id = body.owner_id
            audit(db, session.user_id, bot.org_id, "bot.transfer", bot.id)
            return bot_view(bot)

    @router.get("/bots/{bot_id}/documents")
    def documents(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            accessible_bot(db, bot_id, session.user_id)
            return [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "knowledgebase": d.knowledgebase,
                    "status": d.status,
                    "size": d.size,
                    "chunks": len(d.chunks),
                }
                for d in selected_documents(db, bot_id)
            ]

    @router.post("/bots/{bot_id}/documents")
    def upload(
        bot_id: str,
        request: Request,
        knowledgebase: str = Form("default"),
        file: UploadFile = File(...),
    ):
        import re

        if not re.fullmatch(r"[a-z0-9-]{1,100}", knowledgebase):
            raise HTTPException(422, "Invalid knowledge base name.")
        filename = Path((file.filename or "").replace("\\", "/")).name[:255]
        ext = Path(filename).suffix.lower()
        if ext not in {".pdf", ".docx", ".txt", ".md", ".html", ".htm"}:
            raise HTTPException(
                422, "Upload PDF, DOCX, text, Markdown, or HTML documents."
            )
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
        data = file.file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes or not data:
            raise HTTPException(
                413, "Documents must be nonempty and no larger than 10 MB."
            )
        storage_key = uid() + ext
        object_store(settings).put_bytes(storage_key, data)
        # Recheck permissions after the network transfer, before accepting the file.
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            docs = selected_documents(db, bot_id)
            checksum = hashlib.sha256(data).hexdigest()
            if any(
                d.content_hash == checksum and d.knowledgebase == knowledgebase
                for d in docs
            ):
                return {"duplicate": True}
            if len(docs) >= 100 or any(
                d.filename == filename and d.knowledgebase == knowledgebase
                for d in docs
            ):
                raise HTTPException(
                    409,
                    "Remove the previous file with this name, or reduce the document count below 100.",
                )
            doc = Document(
                id=uid(),
                bot_id=bot_id,
                filename=filename,
                knowledgebase=knowledgebase,
                content_hash=checksum,
                size=len(data),
                storage_key=storage_key,
                embedding_model=docs[0].embedding_model
                if docs
                else settings.embedding_model,
            )
            db.add(doc)
            db.flush()
            job = enqueue(db, bot, "index", {"document_id": doc.id})
            bot.version += 1
            audit(db, session.user_id, bot.org_id, "document.upload", doc.id)
            return {"document_id": doc.id, "job": job}

    @router.delete("/bots/{bot_id}/documents/{document_id}")
    def remove_document(bot_id: str, document_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            doc = db.get(Document, document_id)
            if not doc or doc.bot_id != bot.id:
                raise HTTPException(404)
            doc.active = False
            bot.version += 1
            audit(db, session.user_id, bot.org_id, "document.remove", doc.id)
            return {"removed": True}

    @router.post("/bots/{bot_id}/knowledgebases/rename")
    def rename_knowledgebase(bot_id: str, body: RenameKnowledgebase, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            check_version(bot, body.version)
            docs = ready_documents(db, bot)
            if body.old_name == body.new_name:
                return bot_view(bot)
            if any(d.knowledgebase == body.new_name for d in docs):
                raise HTTPException(
                    409, "The destination knowledge base already has documents."
                )
            for doc in docs:
                if doc.knowledgebase != body.old_name:
                    continue
                # Clone immutable metadata, retaining old IDs for published revisions.
                db.add(
                    Document(
                        bot_id=bot.id,
                        filename=doc.filename,
                        knowledgebase=body.new_name,
                        content_hash=doc.content_hash,
                        storage_key=doc.storage_key,
                        size=doc.size,
                        status="ready",
                        chunks=doc.chunks,
                        embeddings=doc.embeddings,
                        embedding_model=doc.embedding_model,
                    )
                )
                doc.active = False
            config = copy.deepcopy(bot.draft)
            for profile in config["profiles"]:
                if profile["knowledgebase"] == body.old_name:
                    profile["knowledgebase"] = body.new_name
            bot.draft, bot.version = config, bot.version + 1
            audit(db, session.user_id, bot.org_id, "knowledgebase.rename", bot.id)
            return bot_view(bot)

    @router.post("/bots/{bot_id}/publish")
    def publish(bot_id: str, body: Version, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            check_version(bot, body.version)
            validate_config(bot.draft, settings)
            docs = ready_documents(db, bot)
            revision = Revision(
                bot_id=bot.id,
                config=copy.deepcopy(bot.draft),
                document_ids=[d.id for d in docs],
            )
            db.add(revision)
            db.flush()
            bot.published_id = revision.id
            ensure_links(db, bot)
            audit(db, session.user_id, bot.org_id, "bot.publish", revision.id)
            return {
                "revision_id": revision.id,
                "bot_id": bot.id,
                "url": settings.base_url + public_path(db, bot),
            }

    @router.post("/bots/{bot_id}/unpublish")
    def unpublish(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            bot.published_id = None
            audit(db, session.user_id, bot.org_id, "bot.unpublish", bot.id)
            return {"published": False}

    @router.get("/bots/{bot_id}/revisions")
    def revisions(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            accessible_bot(db, bot_id, session.user_id)
            return [
                {"id": r.id, "title": r.config["title"], "created": r.created}
                for r in db.scalars(
                    select(Revision)
                    .where(Revision.bot_id == bot_id)
                    .order_by(Revision.created.desc())
                )
            ]

    @router.post("/bots/{bot_id}/revisions/{revision_id}/activate")
    def rollback(bot_id: str, revision_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            revision = db.get(Revision, revision_id)
            if not revision or revision.bot_id != bot.id:
                raise HTTPException(404)
            validate_config(revision.config, settings)
            bot.published_id = revision.id
            audit(db, session.user_id, bot.org_id, "bot.rollback", revision.id)
            return {"revision_id": revision.id}

    @router.post("/bots/{bot_id}/preview/attachments")
    def preview_attachment(bot_id: str, request: Request, file: UploadFile = File(...)):
        from .uploads import MAX_BYTES, convert_upload, filename_for, reserve_upload
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id)
            if not bot.draft.get("uploads_enabled", False):
                raise HTTPException(422, "Enable file uploads in Settings first.")
            reserve_upload(db, request, settings, bot.org_id, session.user_id)
        filename = filename_for(file.filename)
        value = convert_upload(file.file.read(MAX_BYTES + 1), filename)
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id)
            if not bot.draft.get("uploads_enabled", False):
                raise HTTPException(422, "File uploads are disabled.")
        return {"filename": filename, "text": value["text"]}

    @router.post("/bots/{bot_id}/preview")
    def preview(bot_id: str, body: Preview, request: Request):
        if any(
            set(m) != {"role", "content"}
            or m["role"] not in {"user", "assistant"}
            or not isinstance(m["content"], str)
            or len(m["content"]) > 8000
            for m in body.history
        ):
            raise HTTPException(422, "Invalid preview history.")
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id)
            docs = ready_documents(db, bot)
            profile_for(bot.draft, body.profile)
            from .uploads import attachment_context
            attachment_context(bot.draft, [a.model_dump() for a in body.attachments])
            reserve_calls(db, bot.org_id, settings)
            config = bot.draft
        try:
            return runtime.answer(
                config, docs, body.message, body.profile, body.history,
                attachments=[a.model_dump() for a in body.attachments],
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                502,
                "The model could not answer. Check the operator's model configuration.",
            ) from None

    @router.get("/bots/{bot_id}/evaluation-plan")
    def evaluation_plan(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            bot = accessible_bot(db, bot_id, session.user_id)
            return plan(bot.draft, settings)

    @router.post("/bots/{bot_id}/scenarios/import")
    def import_scenarios(bot_id: str, body: ScenarioCSV, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            accessible_bot(db, bot_id, session.user_id)
        try:
            rows = parse_scenario_csv(body.csv)
            if len(rows) > 120:
                raise ValueError("Import at most 120 questions.")
            return {"scenarios": [scenario_from_scaffold(row) for row in rows]}
        except ValueError:
            raise HTTPException(
                422,
                "Use a CSV with input and expected-answer columns, up to 120 questions.",
            ) from None

    @router.get("/bots/{bot_id}/scenarios.csv")
    def download_scenarios(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            bot = accessible_bot(db, bot_id, session.user_id)
            data = scenarios_to_csv(scaffold_scenarios(bot.draft["scenarios"]))
        return Response(
            data,
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="asklit-scenarios.csv"'
            },
        )

    @router.post("/bots/{bot_id}/scenarios/generate")
    def generate_scenarios(bot_id: str, body: GenerateScenarios, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id)
            docs = ready_documents(db, bot)
            profile = profile_for(bot.draft, body.profile)
            if body.model not in settings.models:
                raise HTTPException(422, "Select an operator-configured model.")
            if not any(
                d.chunks
                and d.knowledgebase == profile["knowledgebase"]
                and (
                    not profile["connected_files"]
                    or d.filename in profile["connected_files"]
                )
                for d in docs
            ):
                raise HTTPException(422, "Add and index sources for this prompt first.")
            reserve_calls(db, bot.org_id, settings)
            audit(db, session.user_id, bot.org_id, "scenarios.generate", bot.id)
        try:
            return {
                "scenarios": generate(runtime, docs, profile, body.model, body.count)
            }
        except Exception:
            raise HTTPException(
                502,
                "Could not generate questions. Try fewer questions or another model.",
            ) from None

    @router.post("/bots/{bot_id}/evaluate")
    def evaluate(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            docs = ready_documents(db, bot)
            config = bot.draft
            if not config["scenarios"]:
                raise HTTPException(422, "Add evaluation scenarios first.")
            run = plan(config, settings)
            if run["total_calls"] > settings.evaluation_max_calls:
                raise HTTPException(
                    422,
                    f"This matrix needs {run['total_calls']} calls. The limit is {settings.evaluation_max_calls}, including judge calls.",
                )
            result = enqueue(
                db,
                bot,
                "evaluate",
                {"config": config, "plan": run, "document_ids": [d.id for d in docs]},
            )
            reserve_calls(db, bot.org_id, settings, run["total_calls"])
            audit(db, session.user_id, bot.org_id, "evaluation.enqueue", result["id"])
            return result

    @router.get("/bots/{bot_id}/jobs")
    def jobs(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            accessible_bot(db, bot_id, session.user_id)
            return [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "status": j.status,
                    "result": j.result,
                    "created": j.created,
                }
                for j in db.scalars(
                    select(Job)
                    .where(Job.bot_id == bot_id)
                    .order_by(Job.created.desc())
                    .limit(30)
                )
            ]

    @router.post("/bots/{bot_id}/export")
    def export_bundle(bot_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            docs = ready_documents(db, bot)
            return enqueue(
                db,
                bot,
                "export",
                {"config": bot.draft, "document_ids": [d.id for d in docs]},
            )

    @router.get("/bots/{bot_id}/jobs/{job_id}/download")
    def download(bot_id: str, job_id: str, request: Request):
        with store.transaction() as db:
            session = login_session(db, request)
            accessible_bot(db, bot_id, session.user_id)
            job = db.get(Job, job_id)
            if (
                not job
                or job.bot_id != bot_id
                or job.kind != "export"
                or job.status != "succeeded"
            ):
                raise HTTPException(404)
            storage_key = job.result["storage_key"]
        try:
            return object_store(settings).download_response(storage_key)
        except FileNotFoundError:
            raise HTTPException(
                410, "This export has expired. Generate it again."
            ) from None

    @router.get("/bots/{bot_id}/workspace")
    def workspace(bot_id: str, request: Request):
        from .portability import workspace_yaml

        with store.transaction() as db:
            session = login_session(db, request)
            bot = accessible_bot(db, bot_id, session.user_id)
            value = workspace_yaml(bot.draft, settings, selected_documents(db, bot_id))
            return Response(
                value,
                media_type="application/yaml",
                headers={
                    "Content-Disposition": 'attachment; filename="asklit-workspace.yaml"'
                },
            )

    @router.post("/bots/{bot_id}/workspace")
    def import_workspace(bot_id: str, body: ImportWorkspace, request: Request):
        from .portability import read_workspace

        with store.transaction() as db:
            session = login_session(db, request, True)
            bot = accessible_bot(db, bot_id, session.user_id, True)
            check_version(bot, body.version)
            try:
                config, missing = read_workspace(body.yaml, settings)
            except (ValueError, TypeError, KeyError):
                raise HTTPException(
                    422, "Invalid or unsupported workspace YAML."
                ) from None
            if selected_documents(db, bot_id):
                raise HTTPException(
                    409,
                    "Import into a new bot so existing documents are not reassigned.",
                )
            bot.draft, bot.title, bot.version = config, config["title"], bot.version + 1
            audit(db, session.user_id, bot.org_id, "workspace.import", bot.id)
            return {
                "bot": bot_view(bot),
                "files_to_reupload": missing,
                "notice": "The hosted model preset is operator-controlled. Reupload source files; custom endpoints and image branding were not imported.",
            }

    return router
