import streamlit as st

from asklit.config import get_secret_value, get_setting
from asklit.db import init_db


init_db()

st.set_page_config(
    page_title=get_setting("app.title", "AskLit"),
    page_icon=get_setting("branding.favicon_url", "💬"),
    layout="centered",
)

chat_page = st.Page(
    "chat_ui.py", title=get_setting("app.title", "AskLit"), icon="💬", default=True
)
login_page = st.Page("login_ui.py", title="Admin Login", icon="🔐")
admin_settings = st.Page("admin/settings.py", title="Admin Settings", icon="⚙️")
admin_kb = st.Page("admin/kb.py", title="Knowledge Base", icon="📚")
admin_logs = st.Page("admin/logs.py", title="Usage Logs", icon="📈")
admin_hash_tool = st.Page("admin/hash_tool.py", title="Password Hash Tool", icon="🔑")


def logout():
    st.session_state["is_admin_authenticated"] = False
    st.rerun()


logout_page = st.Page(logout, title="Logout", icon="🚪")
admin_route = get_secret_value("ADMIN_ROUTE", "admin-login")
disable_admin = str(get_setting("app.disable_admin", "false")).lower() == "true"
public_pages = [chat_page]

if admin_route in st.query_params and not disable_admin:
    st.session_state["admin_unlocked"] = True

if st.session_state.get("is_admin_authenticated") and not disable_admin:
    navigation = st.navigation(
        {
            "Public": public_pages,
            "Admin Management": [
                admin_settings,
                admin_kb,
                admin_logs,
                admin_hash_tool,
            ],
            "Account": [logout_page],
        }
    )
elif st.session_state.get("admin_unlocked") and not disable_admin:
    navigation = st.navigation(
        {"Chat": public_pages, "System": [login_page, admin_hash_tool]}
    )
else:
    navigation = st.navigation(public_pages)

navigation.run()
