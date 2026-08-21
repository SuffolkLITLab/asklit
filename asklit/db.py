import sqlite3
import os

DB_PATH_DEFAULT = os.path.join("data", "app.sqlite3")


def get_db_path():
    return os.environ.get("ASKLIT_DB_PATH", DB_PATH_DEFAULT)


def get_connection(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=None):
    if db_path is None:
        db_path = get_db_path()

    conn = get_connection(db_path=db_path)
    cursor = conn.cursor()

    # Users and Roles
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Settings
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Prompts
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS prompt_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt_key TEXT NOT NULL DEFAULT 'default',
        content TEXT NOT NULL,
        is_active BOOLEAN DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Documents
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        knowledgebase TEXT NOT NULL DEFAULT 'default',
        filename TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        file_size INTEGER NOT NULL,
        content_hash TEXT,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("PRAGMA table_info(prompt_versions)")
    prompt_columns = {row[1] for row in cursor.fetchall()}
    if "prompt_key" not in prompt_columns:
        cursor.execute(
            "ALTER TABLE prompt_versions ADD COLUMN prompt_key TEXT NOT NULL DEFAULT 'default'"
        )

    cursor.execute("PRAGMA table_info(documents)")
    document_columns = {row[1] for row in cursor.fetchall()}
    if "knowledgebase" not in document_columns:
        cursor.execute(
            "ALTER TABLE documents ADD COLUMN knowledgebase TEXT NOT NULL DEFAULT 'default'"
        )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompt_versions_key_active ON prompt_versions (prompt_key, is_active, created_at)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_knowledgebase ON documents (knowledgebase)"
    )

    # Document Chunks
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS document_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        page_number INTEGER,
        FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE
    )
    """)

    # Conversations
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)

    # Messages
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        tokens INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
    )
    """)

    # Usage events
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        event_type TEXT NOT NULL,
        model TEXT,
        tokens_in INTEGER,
        tokens_out INTEGER,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Structured diagnostics for model and retrieval calls.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_call_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        source TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        prompt_key TEXT,
        knowledgebase TEXT,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        error_type TEXT,
        error_message TEXT,
        latency_ms INTEGER,
        tokens_in INTEGER,
        tokens_out INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_call_events_created_at ON ai_call_events (created_at)"
    )

    # Rate limit events
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rate_limit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        identifier TEXT NOT NULL,
        event_type TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
