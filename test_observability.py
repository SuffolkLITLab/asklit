from asklit.db import get_connection
from asklit.observability import log_ai_call_event, safe_error_message


def test_safe_error_message_redacts_credentials():
    message = safe_error_message(
        RuntimeError("Authorization: Bearer-secret API_KEY=sk-abcdefghijklmnop")
    )

    assert "Bearer-secret" not in message
    assert "sk-abcdefghijklmnop" not in message
    assert "[REDACTED]" in message


def test_failed_ai_call_is_persisted(monkeypatch, tmp_path):
    db_path = tmp_path / "app.sqlite3"
    monkeypatch.setenv("ASKLIT_DB_PATH", str(db_path))

    displayed_error = log_ai_call_event(
        run_id="run-123",
        source="experiment_lab",
        provider="azure_apim",
        model="gpt-5.4-mini",
        prompt_key="housing",
        knowledgebase="housing-kb",
        status="failed",
        stage="completion",
        error=RuntimeError("Gateway returned 403"),
        latency_ms=42,
        tokens_in=100,
        tokens_out=0,
    )

    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM ai_call_events WHERE run_id = 'run-123'"
    ).fetchone()
    conn.close()

    assert displayed_error == "Gateway returned 403"
    assert row["status"] == "failed"
    assert row["stage"] == "completion"
    assert row["error_type"] == "RuntimeError"
    assert row["error_message"] == "Gateway returned 403"
