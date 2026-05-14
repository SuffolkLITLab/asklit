import sqlite3
import unittest

from asklit.auth import hash_password, verify_password
from asklit.config import get_nested_value, get_setting
from asklit.db import get_connection, init_db
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
    conn.close()

    case.assertIn("settings", tables)
    case.assertIn("documents", tables)
    case.assertIn("messages", tables)


def test_get_connection_creates_parent_directory(tmp_path):
    case = unittest.TestCase()
    db_path = tmp_path / "new" / "app.sqlite3"

    conn = get_connection(str(db_path))
    conn.execute("CREATE TABLE smoke (id INTEGER)")
    conn.close()

    case.assertTrue(db_path.exists())


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
