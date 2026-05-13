import os
import toml
import streamlit as st
from asklit.db import get_connection

DEFAULT_CONFIG_PATH = os.path.join("config", "defaults.toml")

def load_toml_config(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return toml.load(f)
    return {}

def get_setting(key, default=None):
    """
    Retrieve a setting value. Priority:
    1. SQLite database
    2. Streamlit secrets / Env vars
    3. defaults.toml
    """
    # 1. Check SQLite
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row['value']
    except Exception:
        pass

    # 2. Check Streamlit secrets
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        # Fallback to manual load if st.secrets is not initialized
        secrets = get_secrets_manually()
        if key in secrets:
            return secrets[key]

    # 3. Check defaults.toml
    config = load_toml_config(DEFAULT_CONFIG_PATH)
    # Handle nested keys like 'app.title'
    parts = key.split('.')
    val = config
    for part in parts:
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return default
    return val

def set_setting(key, value):
    """Save a setting to the SQLite database."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, str(value))
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
    try:
        if key_name in st.secrets:
            return st.secrets[key_name]
    except Exception:
        pass
    
    # Fallback to manual load or env
    secrets = get_secrets_manually()
    return secrets.get(key_name) or os.getenv(key_name)

def get_base_url(provider):
    """Retrieve custom base URL for a specific provider from secrets."""
    key_name = f"{provider.upper()}_BASE_URL"
    try:
        if key_name in st.secrets:
            return st.secrets[key_name]
    except Exception:
        pass
    
    # Fallback to manual load or env
    secrets = get_secrets_manually()
    return secrets.get(key_name) or os.getenv(key_name)
