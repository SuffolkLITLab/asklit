import streamlit as st
import hashlib
import hmac
from passlib.hash import pbkdf2_sha256
from asklit.config import get_secret_value, get_setting


def hash_password(password):
    """Hash a password for storage in Streamlit secrets."""
    return pbkdf2_sha256.hash(password)


def verify_password(password, stored_hash):
    """Verify modern passlib hashes and legacy SHA-256 hashes."""
    if not password or not stored_hash:
        return False

    if stored_hash.startswith("$pbkdf2-sha256$"):
        return pbkdf2_sha256.verify(password, stored_hash)

    legacy_hash = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(legacy_hash, stored_hash)


def is_admin():
    """Check if the current session is an admin session."""
    return bool(st.session_state.get("is_admin_authenticated"))


def check_password():
    """Returns True if the user had the correct password."""
    access_mode = get_setting("app.access_mode", "public")

    if access_mode == "public":
        return True

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        shared_hash = get_secret_value("SHARED_PASSWORD_HASH", None)
        if verify_password(st.session_state["password"], shared_hash):
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # don't store password
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        # First run, show input for password.
        st.text_input(
            "Password", type="password", on_change=password_entered, key="password"
        )
        return False
    elif not st.session_state["password_correct"]:
        # Password incorrect, show input + error.
        st.text_input(
            "Password", type="password", on_change=password_entered, key="password"
        )
        st.error("😕 Password incorrect")
        return False
    else:
        # Password correct.
        return True


def admin_login():
    """Show admin login form."""

    def verify_admin():
        admin_hash = get_secret_value("ADMIN_PASSWORD_HASH", None)
        if verify_password(st.session_state["admin_password"], admin_hash):
            st.session_state["is_admin_authenticated"] = True
            del st.session_state["admin_password"]
        else:
            st.session_state["is_admin_authenticated"] = False
            st.error("Admin password incorrect")

    if not is_admin():
        st.text_input(
            "Admin Password",
            type="password",
            on_change=verify_admin,
            key="admin_password",
        )
        return False
    return True
