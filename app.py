import streamlit as st
from asklit.config import get_secret_value, get_setting
from asklit.db import init_db

init_db()

# Set page config once at the very top
st.set_page_config(
    page_title=get_setting("app.title", "AskLit"),
    page_icon=get_setting("branding.favicon_url", "💬"),
    layout="centered",
)

# 1. Define all possible pages
chat_page = st.Page(
    "chat_ui.py", title=get_setting("app.title", "AskLit"), icon="💬", default=True
)

login_page = st.Page("login_ui.py", title="Admin Login", icon="🔐")

scaffold_page = st.Page("scaffold.py", title="Project Scaffolder", icon="🏗️")

admin_settings = st.Page("admin/settings.py", title="Admin Settings", icon="⚙️")
admin_kb = st.Page("admin/kb.py", title="Knowledge Base", icon="📚")
admin_logs = st.Page("admin/logs.py", title="Usage Logs", icon="📈")
admin_hash_tool = st.Page("admin/hash_tool.py", title="Password Hash Tool", icon="🔑")


def logout():
    st.session_state["is_admin_authenticated"] = False
    st.rerun()


logout_page = st.Page(logout, title="Logout", icon="🚪")

# 2. Build the navigation based on authentication status
# We use a secret route name from secrets to "unlock" the admin login page
# Example: If ADMIN_ROUTE = "manage", visiting /?manage will show the login page
admin_route = get_secret_value("ADMIN_ROUTE", "admin-login")
disable_admin = str(get_setting("app.disable_admin", "false")).lower() == "true"
enable_scaffolder = str(get_setting("app.enable_scaffolder", "false")).lower() == "true"
public_pages = [chat_page]
if enable_scaffolder:
    public_pages.append(scaffold_page)

if admin_route in st.query_params and not disable_admin:
    st.session_state["admin_unlocked"] = True
    # We don't clear it immediately to ensure the initial load captures it
    # but we can set a flag.

if st.session_state.get("is_admin_authenticated") and not disable_admin:
    # Admin View: Show everything
    pg = st.navigation(
        {
            "Public": public_pages,
            "Admin Management": [admin_settings, admin_kb, admin_logs, admin_hash_tool],
            "Account": [logout_page],
        }
    )
elif st.session_state.get("admin_unlocked") and not disable_admin:
    # Hidden Login View: Show Chat + Login + Setup Tools
    pg = st.navigation({"Chat": public_pages, "System": [login_page, admin_hash_tool]})
else:
    # Pure Public View: Only the production chat flow by default.
    pg = st.navigation(public_pages)

# 3. Run navigation
pg.run()
