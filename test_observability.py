from concurrent.futures import ThreadPoolExecutor

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


def test_concurrent_diagnostic_writes_do_not_lock_database(monkeypatch, tmp_path):
    db_path = tmp_path / "classroom.sqlite3"
    monkeypatch.setenv("ASKLIT_DB_PATH", str(db_path))

    def write_event(index):
        log_ai_call_event(
            run_id=f"run-{index}",
            source="experiment_lab",
            provider="openai",
            model="test-model",
            status="succeeded",
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(write_event, range(20)))

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM ai_call_events").fetchone()[0]
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()

    assert count == 20
    assert journal_mode == "wal"
