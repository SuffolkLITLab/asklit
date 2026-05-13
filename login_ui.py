import streamlit as st
import hashlib
from asklit.auth import is_admin

def login_page():
    st.title("🔐 Admin Login")
    
    with st.form("login_form"):
        password = st.text_input("Admin Password", type="password")
        submit = st.form_submit_button("Login")
        
        if submit:
            if not password:
                st.error("Please enter a password.")
            else:
                # Check against secret
                admin_hash = st.secrets.get("ADMIN_PASSWORD_HASH")
                if not admin_hash:
                    st.error("ADMIN_PASSWORD_HASH not found in secrets.")
                elif hashlib.sha256(password.encode()).hexdigest() == admin_hash:
                    st.session_state["is_admin_authenticated"] = True
                    st.success("Login successful!")
                    st.rerun()
                else:
                    st.error("Invalid password.")

if __name__ == "__main__":
    login_page()
