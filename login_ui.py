import streamlit as st
from asklit.auth import verify_password
from asklit.config import get_secret_value


def login_page():
    st.title("🔐 Admin Login")

    with st.form("login_form"):
        password = st.text_input("Admin Password", type="password")
        submit = st.form_submit_button("Login")

        if submit:
            if not password:
                st.error("Please enter a password.")
            else:
                admin_hash = get_secret_value("ADMIN_PASSWORD_HASH", None)
                if not admin_hash:
                    st.error("ADMIN_PASSWORD_HASH not found in secrets.")
                elif verify_password(password, admin_hash):
                    st.session_state["is_admin_authenticated"] = True
                    st.success("Login successful!")
                    st.rerun()
                else:
                    st.error("Invalid password.")


if __name__ == "__main__":
    login_page()
