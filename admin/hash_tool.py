import streamlit as st
from asklit.auth import hash_password


def hash_tool_page():
    st.title("🔑 Password Hash Generator")
    st.write("""
        Use this tool to generate a secure hash for your `secrets.toml` or environment variables. 
        Enter the password you want to use, copy the resulting hash, and paste it into your configuration.
    """)

    password = st.text_input("Enter password to hash", type="password")

    if password:
        pw_hash = hash_password(password)

        st.subheader("Your Secure Hash:")
        st.code(pw_hash, language="text")

        st.warning("""
            **How to use this:**
            1. Copy the long string of letters and numbers above.
            2. Open your `.streamlit/secrets.toml` file.
            3. Paste it as the value for `ADMIN_PASSWORD_HASH` or `SHARED_PASSWORD_HASH`.
            
            Example:
            `ADMIN_PASSWORD_HASH = "..."`
        """)


if __name__ == "__main__":
    hash_tool_page()
