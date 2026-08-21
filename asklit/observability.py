import logging
import re

from asklit.db import get_connection, init_db


logger = logging.getLogger("asklit.ai")
MAX_ERROR_LENGTH = 1200
SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[-_ ]?key|authorization|subscription[-_ ]?key)(\s*[:=]\s*)([^\s,;]+)"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
]


def safe_error_message(error):
    """Return a useful bounded error message with common credential forms removed."""
    message = str(error).replace("\n", " ").strip()
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 3:
            message = pattern.sub(r"\1\2[REDACTED]", message)
        else:
            message = pattern.sub("[REDACTED]", message)
    return message[:MAX_ERROR_LENGTH] or "No error details were provided."


def log_ai_call_event(
    *,
    run_id,
    source,
    provider,
    model,
    status,
    stage="completion",
    prompt_key=None,
    knowledgebase=None,
    error=None,
    latency_ms=None,
    tokens_in=None,
    tokens_out=None,
):
    """Persist safe AI-call diagnostics and mirror them to the server log."""
    error_type = type(error).__name__ if error is not None else None
    error_message = safe_error_message(error) if error is not None else None
    log_method = logger.error if status == "failed" else logger.info
    log_method(
        "AI call %s run_id=%s source=%s stage=%s provider=%s model=%s "
        "prompt=%s knowledgebase=%s latency_ms=%s error_type=%s error=%s",
        status,
        run_id,
        source,
        stage,
        provider,
        model,
        prompt_key,
        knowledgebase,
        latency_ms,
        error_type,
        error_message,
    )

    conn = None
    try:
        init_db()
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO ai_call_events
                (run_id, source, provider, model, prompt_key, knowledgebase,
                 status, stage, error_type, error_message, latency_ms,
                 tokens_in, tokens_out)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source,
                provider,
                model,
                prompt_key,
                knowledgebase,
                status,
                stage,
                error_type,
                error_message,
                latency_ms,
                tokens_in,
                tokens_out,
            ),
        )
        conn.commit()
    except Exception as logging_error:
        logger.error(
            "Failed to persist AI call run_id=%s error=%s",
            run_id,
            safe_error_message(logging_error),
        )
    finally:
        if conn is not None:
            conn.close()

    return error_message
