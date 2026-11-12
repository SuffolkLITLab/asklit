"""Durable PostgreSQL worker. Run independently from the API, with any replica count."""

import argparse
import copy
import multiprocessing
import tempfile
from pathlib import Path
import threading
import time
import zipfile

from sqlalchemy import delete, select, update

from asklit.experiments import (
    build_rubric_judge_messages,
    combine_grades,
    deterministic_grade,
    parse_rubric_grade,
    resolve_rubric_rules,
)
from .models import (
    AccessGrant,
    Bot,
    Conversation,
    Document,
    Invitation,
    Job,
    LoginSession,
    RateBucket,
    Store,
    Turn,
    uid,
)
from .runtime import MAX_CHUNKS, Runtime, selected_documents
from .settings import Settings
from .storage import object_store, cleanup_deleted_objects
from .models import Organization


def sweep(store):
    """Expire secrets/transcripts and fail interrupted work without replaying charges."""
    now = time.time()
    with store.transaction() as db:
        # SKIP LOCKED makes housekeeping safe while another worker commits a job.
        stale = list(
            db.scalars(
                select(Job)
                .where(Job.status == "running", Job.heartbeat < now - 120)
                .with_for_update(skip_locked=True)
            )
        )
        for job in stale:
            job.status, job.result = (
                "failed",
                {"error": "Worker interrupted. Start a new task to retry."},
            )
            if job.kind == "index":
                doc = db.get(Document, job.payload["document_id"])
                if doc and doc.status != "ready":
                    doc.status = "failed"
        expired = select(Conversation.id).where(
            Conversation.expires < now, Conversation.busy_until < now
        )
        db.execute(delete(Turn).where(Turn.conversation_id.in_(expired)))
        db.execute(delete(Conversation).where(Conversation.id.in_(expired)))
        db.execute(delete(AccessGrant).where(
            AccessGrant.expires < now,
            AccessGrant.token_hash.not_in(select(Conversation.access_grant).where(Conversation.access_grant.is_not(None))),
        ))
        db.execute(delete(LoginSession).where(LoginSession.expires < now))
        db.execute(delete(Invitation).where(Invitation.expires < now))
        db.execute(delete(RateBucket).where(RateBucket.expires < now))


def claim(store):
    with store.transaction() as db:
        job = db.scalar(
            select(Job)
            .where(Job.status == "queued", Job.bot_id.in_(select(Bot.id).where(Bot.org_id.in_(select(Organization.id).where(Organization.archived.is_(False))))))
            .order_by(Job.created)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status, job.started, job.heartbeat = "running", time.time(), time.time()
        return job


def heartbeat(store, job_id, stop):
    while not stop.wait(15):
        try:
            with store.transaction() as db:
                db.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == "running")
                    .values(heartbeat=time.time())
                )
        except Exception:
            # A transient outage should not permanently stop heartbeat attempts.
            # A sustained outage still expires the lease through housekeeping.
            continue


def index_document(job, store, settings, runtime):
    from asklit.ingestion import chunk_pages, extract_text

    with store.transaction() as db:
        doc = db.get(Document, job.payload["document_id"])
        if not doc or doc.bot_id != job.bot_id:
            raise ValueError("Invalid document.")
        model = doc.embedding_model
    with object_store(settings).materialize(doc.storage_key) as path:
        if path.suffix == ".docx":
            with zipfile.ZipFile(path) as archive:
                if (
                    sum(info.file_size for info in archive.infolist())
                    > 50 * 1024 * 1024
                ):
                    raise ValueError("Expanded document exceeds 50 MB.")
        text, pages = extract_text(str(path))
    if len(text) > 2_000_000:
        raise ValueError("Document contains more than two million characters.")
    chunks = chunk_pages(pages)
    if not chunks or len(chunks) > MAX_CHUNKS:
        raise ValueError("Document has no readable text or exceeds the chunk limit.")
    vectors = runtime.embedder([c["content"] for c in chunks], model)
    if len(vectors) != len(chunks):
        raise ValueError("Embedding count does not match the document.")
    with store.transaction() as db:
        current = db.scalar(select(Job).where(Job.id == job.id).with_for_update())
        if current.status != "running":
            raise RuntimeError("This job is no longer active.")
        bot = db.scalar(select(Bot).where(Bot.id == job.bot_id).with_for_update())
        total = sum(
            len(d.chunks) for d in selected_documents(db, bot.id) if d.id != doc.id
        )
        if total + len(chunks) > MAX_CHUNKS:
            raise ValueError("Bot exceeds the total chunk limit.")
        current_doc = db.get(Document, doc.id)
        current_doc.chunks, current_doc.embeddings, current_doc.status = (
            chunks,
            vectors,
            "ready",
        )
        current.status, current.result = (
            "succeeded",
            {"chunks": len(chunks), "filename": doc.filename},
        )
    return {"chunks": len(chunks)}


def evaluate(job, docs, runtime, progress=None):
    from .evaluation import plan, combinations
    from asklit.llm import estimate_tokens

    config = job.payload["config"]
    run = job.payload.get("plan") or plan(config, runtime.settings)
    rows = []
    for scenario, profile, source, model in combinations(config, run):
        current = copy.deepcopy(config)
        current["model_preset"], current["top_k"] = model, run["top_k"]
        current["profiles"] = [
            {
                **profile,
                "knowledgebase": source["knowledgebase"],
                "connected_files": source["connected_files"],
            }
        ]
        question, expected = scenario["input"], scenario["expected"]
        started = time.perf_counter()
        result = {"answer": "", "sources": []}
        error = None
        judge_elapsed = 0
        judge_tokens = 0
        rules = resolve_rubric_rules(config["rubrics"], expected)
        grade = {"passed": None, "score": None, "reason": "Answer failed"}
        stage = "answer"
        try:
            result = runtime.answer(current, docs, question, profile["key"])
            answer_elapsed = time.perf_counter() - started
            deterministic = deterministic_grade(result["answer"], expected)
            rubric = None
            if rules:
                stage = "judge"
                judge_start = time.perf_counter()
                messages = build_rubric_judge_messages(
                    question,
                    result["answer"],
                    "\n".join(rules),
                    context_chunks=result["sources"],
                )
                judged = runtime.call(messages, run["judge_model"])
                judge_elapsed = time.perf_counter() - judge_start
                judge_tokens = sum(
                    estimate_tokens(m["content"]) for m in messages
                ) + estimate_tokens(judged)
                rubric = parse_rubric_grade(judged)
            grade = combine_grades(deterministic, rubric)
        except Exception as exc:
            from .budget import BillingError
            if isinstance(exc, BillingError):
                raise
            answer_elapsed = time.perf_counter() - started
            error = f"{stage.capitalize()} failed ({type(exc).__name__})."
            grade = {"passed": None, "score": None, "reason": error}
        rows.append(
            {
                "profile": profile["label"],
                "prompt_key": profile["key"],
                "knowledgebase": source["knowledgebase"],
                "source_profile": source["key"],
                "source_label": source["label"],
                "model": model,
                "model_name": runtime.settings.models[model]["model"],
                "scenario": scenario.get("description") or question,
                "metadata": scenario.get("metadata", {}),
                "input": question,
                "expected": expected,
                "judge_model": run["judge_model"] if rules else "",
                "error": error,
                "elapsed": round(answer_elapsed, 3),
                "judge_elapsed": round(judge_elapsed, 3),
                "estimated_tokens": estimate_tokens(question)
                + estimate_tokens(profile["prompt"])
                + estimate_tokens(result["answer"])
                + sum(estimate_tokens(c["content"]) for c in result["sources"]),
                "judge_tokens": judge_tokens,
                **result,
                **grade,
            }
        )
        if progress:
            progress(rows, run["answers"])
    return {"rows": rows, "completed": len(rows), "total": run["answers"], "plan": run}


def run_one(store, settings, runtime, job=None):
    if job is None:
        sweep(store)
        cleanup_deleted_objects(settings, store)
        job = claim(store)
    if not job:
        return False
    stop = threading.Event()
    beat = threading.Thread(target=heartbeat, args=(store, job.id, stop), daemon=True)
    beat.start()
    try:
        if job.kind == "index":
            result = index_document(job, store, settings, runtime)
        else:
            with store.transaction() as db:
                docs = selected_documents(db, job.bot_id, job.payload["document_ids"])
            if job.kind == "evaluate":

                def progress(rows, total):
                    with store.transaction() as db:
                        db.execute(
                            update(Job)
                            .where(Job.id == job.id, Job.status == "running")
                            .values(
                                result={
                                    "rows": rows,
                                    "completed": len(rows),
                                    "total": total,
                                }
                            )
                        )

                from .usage import meter
                from .models import Bot
                with store.transaction() as db:
                    usage_bot = db.get(Bot, job.bot_id)
                with meter(store, usage_bot, "evaluation"):
                    result = evaluate(job, docs, runtime, progress)
            elif job.kind == "export":
                from .portability import build_export

                key = uid() + ".zip"
                with tempfile.TemporaryDirectory(prefix="asklit-export-") as directory:
                    path = Path(directory) / key
                    build_export(job.payload["config"], docs, settings, path)
                    object_store(settings).put_file(key, path)
                result = {"storage_key": key}
            else:
                raise ValueError("Unknown job kind.")
        with store.transaction() as db:
            db.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == "running")
                .values(status="succeeded", result=result)
            )
    except Exception as exc:
        from .budget import BillingError
        # Deliberately do not persist exception text: providers may echo credentials or documents.
        with store.transaction() as db:
            current = db.scalar(select(Job).where(Job.id == job.id).with_for_update())
            if current.status == "running":
                current.status, current.result = (
                    "failed",
                    {
                        "error": exc.detail if isinstance(exc, BillingError) else f"{job.kind.capitalize()} failed ({type(exc).__name__}). Check the source file or operator configuration, then retry.",
                        "code": exc.code if isinstance(exc, BillingError) else "job_failed"
                    },
                )
                if job.kind == "index":
                    doc = db.get(Document, job.payload["document_id"])
                    if doc and doc.status != "ready":
                        doc.status = "failed"
    finally:
        stop.set()
        beat.join(timeout=2)
    return True


def execute_claimed(settings, job_id):
    store = Store(settings.database_url)
    with store.transaction() as db:
        job = db.get(Job, job_id)
    if job and job.status == "running":
        run_one(store, settings, Runtime(settings), job=job)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings.database_url)
    context = multiprocessing.get_context("spawn")
    while True:
        sweep(store)
        cleanup_deleted_objects(settings, store)
        job = claim(store)
        if job:
            # Isolate parsers/model workloads and enforce a hard wall-clock bound.
            # A killed billed job is marked failed; it is never automatically retried.
            process = context.Process(target=execute_claimed, args=(settings, job.id))
            process.start()
            process.join(timeout=settings.job_timeout_seconds)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join()
            with store.transaction() as db:
                current = db.scalar(
                    select(Job).where(Job.id == job.id).with_for_update()
                )
                if current.status == "running":
                    current.status = "failed"
                    current.result = {
                        "error": "Worker exited or exceeded its time limit. Start a new task to retry."
                    }
                    if current.kind == "index":
                        doc = db.get(Document, current.payload["document_id"])
                        if doc and doc.status != "ready":
                            doc.status = "failed"
        if args.once:
            return
        if not job:
            time.sleep(2)


if __name__ == "__main__":
    main()
