import os
import sqlite3
import toml
import streamlit as st
from asklit.db import get_connection

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DEFAULT_CONFIG_PATH = os.path.join("config", "defaults.toml")
_MISSING = object()


def load_toml_config(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return toml.load(f)
    return {}


def get_nested_value(config, key, default=_MISSING):
    parts = key.split(".")
    val = config
    for part in parts:
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return default
    return val


def get_secret_value(key, default=_MISSING):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        secrets = get_secrets_manually()
    else:
        secrets = get_secrets_manually()

    if key in secrets:
        return secrets[key]

    nested_value = get_nested_value(secrets, key, _MISSING)
    if nested_value is not _MISSING:
        return nested_value

    env_key = key.upper().replace(".", "_")
    return os.getenv(env_key, default)


def get_setting(key, default=None):
    """
    Retrieve a setting value. Priority:
    1. SQLite database
    2. Streamlit secrets / Env vars
    3. defaults.toml
    """
    # 1. Check SQLite
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return row["value"]
    except sqlite3.Error:
        row = None
    finally:
        if conn is not None:
            conn.close()

    # 2. Check Streamlit secrets / environment
    secret_value = get_secret_value(key, _MISSING)
    if secret_value is not _MISSING:
        return secret_value

    # 3. Check defaults.toml
    config = load_toml_config(DEFAULT_CONFIG_PATH)
    return get_nested_value(config, key, default)


def set_setting(key, value):
    """Save a setting to the SQLite database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_secrets_manually():
    """Manually load secrets from .streamlit/secrets.toml if st.secrets is unavailable."""
    path = os.path.join(".streamlit", "secrets.toml")
    if os.path.exists(path):
        return toml.load(path)
    return {}


def get_api_key(provider):
    """Retrieve API key for a specific provider from secrets."""
    key_name = f"{provider.upper()}_API_KEY"
    return get_secret_value(key_name, None)


def get_base_url(provider):
    """Retrieve a provider URL override from secrets or generated model config."""
    key_name = f"{provider.upper()}_BASE_URL"
    secret_url = get_secret_value(key_name, _MISSING)
    if secret_url is not _MISSING and str(secret_url).strip():
        return str(secret_url).strip()

    configured_provider = str(get_setting("model.provider", "")).strip()
    if str(provider).strip() == configured_provider:
        configured_url = get_setting("model.base_url", None)
        if configured_url and str(configured_url).strip():
            return str(configured_url).strip()
    return None
