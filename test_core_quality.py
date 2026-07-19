import sqlite3
import unittest

from asklit.auth import hash_password, verify_password
from asklit.config import get_nested_value, get_setting
from asklit.db import get_connection, init_db
from asklit.prompts import load_prompt_configs, save_new_prompt, get_active_prompt
from asklit.ui import escape_html, safe_url


def test_init_db_creates_parent_directory_and_tables(tmp_path):
    case = unittest.TestCase()
    db_path = tmp_path / "nested" / "app.sqlite3"

    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
    }
    prompt_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(prompt_versions)").fetchall()
    }
    conn.close()

    case.assertIn("settings", tables)
    case.assertIn("documents", tables)
    case.assertIn("messages", tables)
    case.assertIn("knowledgebase", columns)
    case.assertIn("prompt_key", prompt_columns)


def test_get_connection_creates_parent_directory(tmp_path):
    case = unittest.TestCase()
    db_path = tmp_path / "new" / "app.sqlite3"

    conn = get_connection(str(db_path))
    conn.execute("CREATE TABLE smoke (id INTEGER)")
    conn.close()

    case.assertTrue(db_path.exists())


def test_init_db_migrates_existing_prompt_and_document_rows(tmp_path):
    case = unittest.TestCase()
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE prompt_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    conn.execute(
        "INSERT INTO prompt_versions (content, is_active) VALUES ('Legacy prompt', 1)"
    )
    conn.execute("""
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            content_hash TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    conn.execute("""
        INSERT INTO documents
            (id, filename, file_path, file_type, file_size, status)
        VALUES ('legacy-doc', 'legacy.txt', 'data/uploads/legacy.txt', '.txt', 10, 'indexed')
        """)
    conn.commit()
    conn.close()

    init_db(str(db_path))

    conn = sqlite3.connect(db_path)
    prompt_key = conn.execute(
        "SELECT prompt_key FROM prompt_versions WHERE id = 1"
    ).fetchone()[0]
    knowledgebase = conn.execute(
        "SELECT knowledgebase FROM documents WHERE id = 'legacy-doc'"
    ).fetchone()[0]
    conn.close()

    case.assertEqual(prompt_key, "default")
    case.assertEqual(knowledgebase, "default")


def test_prompt_configs_support_multiple_yaml_files(monkeypatch, tmp_path):
    case = unittest.TestCase()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "default_system_prompt.yml").write_text(
        """
label: Public Benefits
knowledgebase:
  name: benefits
  files:
    - snap.pdf
prompt: Benefits prompt.
conversation starters:
  - Start benefits
""",
        encoding="utf-8",
    )
    (prompts_dir / "housing.yml").write_text(
        """
label: Housing
knowledgebase: housing
connected_files:
  - eviction.md
prompt: Housing prompt.
""",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    configs = load_prompt_configs()

    case.assertEqual([config["key"] for config in configs], ["default", "housing"])
    case.assertEqual(configs[0]["knowledgebase"], "benefits")
    case.assertEqual(configs[0]["connected_files"], ["snap.pdf"])
    case.assertEqual(configs[1]["knowledgebase"], "housing")
    case.assertEqual(configs[1]["connected_files"], ["eviction.md"])


def test_prompt_overrides_are_scoped_by_prompt_key(monkeypatch, tmp_path):
    case = unittest.TestCase()
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "default_system_prompt.yml").write_text(
        "prompt: Default file prompt.\n", encoding="utf-8"
    )
    (prompts_dir / "housing.yml").write_text(
        "prompt: Housing file prompt.\n", encoding="utf-8"
    )
    db_path = tmp_path / "data" / "app.sqlite3"
    init_db(str(db_path))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASKLIT_DB_PATH", str(db_path))

    save_new_prompt("Housing override.", prompt_key="housing")

    case.assertEqual(get_active_prompt("default"), "Default file prompt.")
    case.assertEqual(get_active_prompt("housing"), "Housing override.")


def test_settings_fall_back_to_environment(monkeypatch, tmp_path):
    case = unittest.TestCase()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASKLIT_DB_PATH", str(tmp_path / "missing" / "app.sqlite3"))
    monkeypatch.setenv("APP_TITLE", "Env Title")

    case.assertEqual(get_setting("app.title", "Default"), "Env Title")


def test_get_nested_value_supports_dotted_keys():
    case = unittest.TestCase()
    config = {"app": {"title": "Nested"}}

    case.assertEqual(get_nested_value(config, "app.title"), "Nested")
    case.assertEqual(get_nested_value(config, "app.missing", "Default"), "Default")


def test_password_verification_supports_modern_and_legacy_hashes():
    case = unittest.TestCase()
    modern_hash = hash_password("correct horse")
    legacy_hash = "ef92b778bafe771e89245b89ecbc08a44a4e166c06659911881f383d4473e94f"

    case.assertTrue(verify_password("correct horse", modern_hash))
    case.assertFalse(verify_password("wrong horse", modern_hash))
    case.assertTrue(verify_password("password123", legacy_hash))
    case.assertFalse(verify_password("password124", legacy_hash))


def test_html_helpers_escape_text_and_reject_unsafe_urls():
    case = unittest.TestCase()

    case.assertEqual(
        escape_html('<img src=x onerror="bad">'),
        "&lt;img src=x onerror=&quot;bad&quot;&gt;",
    )
    case.assertEqual(
        safe_url("https://example.com/logo.png"),
        "https://example.com/logo.png",
    )
    case.assertEqual(safe_url("javascript:alert(1)"), "")
