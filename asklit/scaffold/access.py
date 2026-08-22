"""Password gate for the scaffolder areas that spend model credits.

Uploading documents, writing a prompt, and exporting a project cost the host
nothing. The Chat preview and the Evaluate step bill real completions to the
operator's key, so those two steps sit behind a shared password the operator
sets in Streamlit secrets. When no password is configured the gate stays open, which
keeps local development and existing deployments working unchanged.
"""

import hmac
import time

import streamlit as st

from asklit.auth import verify_password
from asklit.config import get_secret_value

PASSWORD_SECRET_KEYS = ("SCAFFOLD_PASSWORD", "scaffold.password")
PASSWORD_HASH_SECRET_KEYS = ("SCAFFOLD_PASSWORD_HASH", "scaffold.password_hash")
SESSION_KEY = "scaffold_access_granted"
ATTEMPTS_KEY = "scaffold_access_attempts"
LOCKED_UNTIL_KEY = "scaffold_access_locked_until"
MAX_ATTEMPTS_BEFORE_PAUSE = 5
LOCKOUT_SECONDS = 60


def _first_configured_secret(keys):
    for key in keys:
        value = get_secret_value(key, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def configured_password():
    return _first_configured_secret(PASSWORD_SECRET_KEYS)


def configured_password_hash():
    return _first_configured_secret(PASSWORD_HASH_SECRET_KEYS)


def access_is_required():
    """Return whether the operator configured a scaffolder password."""
    return bool(configured_password_hash() or configured_password())


def verify_scaffold_password(password):
    """Check an entered password against a hashed or plain configured secret."""
    password = str(password or "")
    if not password:
        return False
    stored_hash = configured_password_hash()
    if stored_hash:
        return verify_password(password, stored_hash)
    stored_plain = configured_password()
    if stored_plain:
        return hmac.compare_digest(password, stored_plain)
    return False


def _seconds_remaining():
    locked_until = st.session_state.get(LOCKED_UNTIL_KEY, 0)
    return max(0, int(locked_until - time.time()))


def _submit_password():
    entered = st.session_state.get("scaffold_access_password", "")
    st.session_state["scaffold_access_password"] = ""
    if verify_scaffold_password(entered):
        st.session_state[SESSION_KEY] = True
        st.session_state[ATTEMPTS_KEY] = 0
        st.session_state[LOCKED_UNTIL_KEY] = 0
        return
    attempts = st.session_state.get(ATTEMPTS_KEY, 0) + 1
    st.session_state[ATTEMPTS_KEY] = attempts
    st.session_state[SESSION_KEY] = False
    if attempts >= MAX_ATTEMPTS_BEFORE_PAUSE:
        st.session_state[LOCKED_UNTIL_KEY] = time.time() + LOCKOUT_SECONDS
        st.session_state[ATTEMPTS_KEY] = 0


def require_access(area_label):
    """Render the gate and return whether this session may spend model credits."""
    if not access_is_required():
        return True
    if st.session_state.get(SESSION_KEY):
        return True

    st.header(area_label)
    st.info(
        f"**{area_label}** makes real model calls billed to this scaffolder's "
        "account, so it is password protected. Ask your instructor or the "
        "AskLit administrator for the shared access password."
    )
    remaining = _seconds_remaining()
    if remaining:
        st.error(
            f"Too many incorrect attempts. Try again in about {remaining} seconds."
        )
        return False

    st.text_input(
        "Scaffolder access password",
        type="password",
        key="scaffold_access_password",
        on_change=_submit_password,
    )
    if st.session_state.get(SESSION_KEY) is False:
        st.error("That password is not correct.")
    st.caption(
        "Uploading documents, writing prompts, and exporting a project stay open "
        "without a password."
    )
    return False


def render_access_status():
    """Show the sidebar lock state so students know why a step is blocked."""
    if not access_is_required():
        return
    if st.session_state.get(SESSION_KEY):
        st.sidebar.caption("🔓 Chat and Evaluate unlocked for this session.")
        if st.sidebar.button("Lock model access"):
            st.session_state[SESSION_KEY] = False
            st.rerun()
    else:
        st.sidebar.caption("🔒 Chat and Evaluate need the shared access password.")
