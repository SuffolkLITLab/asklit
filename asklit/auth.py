import streamlit as st
import hashlib
from asklit.config import get_setting

def check_password():
    """Returns True if the user had the correct password."""
    access_mode = get_setting("app.access_mode", "public")
    
    if access_mode == "public":
        return True

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if hashlib.sha256(st.session_state["password"].encode()).hexdigest() == st.secrets["SHARED_PASSWORD_HASH"]:
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

def is_admin():
    """Check if the current session is an admin session."""
    if "is_admin" not in st.session_state:
        return False
    return st.session_state["is_admin"]

def admin_login():
    """Show admin login form."""
    def verify_admin():
        if hashlib.sha256(st.session_state["admin_password"].encode()).hexdigest() == st.secrets["ADMIN_PASSWORD_HASH"]:
            st.session_state["is_admin"] = True
            del st.session_state["admin_password"]
        else:
            st.session_state["is_admin"] = False
            st.error("Admin password incorrect")

    if not is_admin():
        st.text_input("Admin Password", type="password", on_change=verify_admin, key="admin_password")
        return False
    return True
