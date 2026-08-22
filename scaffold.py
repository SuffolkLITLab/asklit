"""Streamlit entrypoint for the AskLit Project Scaffolder.

The workflow lives in ``asklit.scaffold``, one module per step. This module
routes the selected step to its renderer and applies the access gate that
protects the steps which spend the host's model credits.
"""

import streamlit as st

from asklit.config import get_secret_value
from asklit.scaffold import access
from asklit.scaffold.config import default_scaffold_config, ensure_model_defaults
from asklit.scaffold.step_chat import render_chat_step
from asklit.scaffold.step_evaluate import render_evaluate_step
from asklit.scaffold.step_export import render_export_step
from asklit.scaffold.step_knowledge import render_knowledge_step
from asklit.scaffold.step_prompt import render_prompt_step
from asklit.scaffold.ui import (
    BILLED_STEPS,
    initialize_scaffold_storage,
    render_next_step_button,
    render_step_selector,
    render_workspace_controls,
)
from asklit.ui import escape_html, safe_url

# Re-exported so existing imports of the pre-split module keep working.
from asklit.scaffold.bundle import (  # noqa: F401
    DEFAULT_REPO_NAME,
    create_bundle,
    generate_deployment_secrets,
    zip_directory,
)
from asklit.scaffold.workspace import (  # noqa: F401
    export_workspace_yaml,
    import_workspace_yaml,
    sanitize_export_config,
)

STEP_RENDERERS = {
    "knowledge": render_knowledge_step,
    "prompt": render_prompt_step,
    "chat": render_chat_step,
    "evaluate": render_evaluate_step,
    "export": render_export_step,
}
BILLED_STEP_LABELS = {"chat": "Chat preview", "evaluate": "Evaluate"}


def render_sidebar_branding():
    """Brand the scaffolder itself from operator-configured secrets."""
    logo_url = safe_url(
        get_secret_value(
            "branding.logo_url",
            "https://github.com/SuffolkLITLab/logos/raw/main/current-logo/png/lit-lab-logo-large.png",
        )
    )
    homepage_url = safe_url(
        get_secret_value("branding.homepage_url", "https://suffolklitlab.org")
    )
    if not logo_url:
        return
    st.sidebar.markdown(
        f'<a href="{escape_html(homepage_url)}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{escape_html(logo_url)}" width="150"></a>',
        unsafe_allow_html=True,
    )
    st.sidebar.divider()


def render_step(step_key):
    """Dispatch one step, gating the ones that make billed model calls."""
    if step_key in BILLED_STEPS and not access.require_access(
        BILLED_STEP_LABELS[step_key]
    ):
        return False
    STEP_RENDERERS[step_key]()
    return True


def main():
    render_sidebar_branding()
    st.title("🧪 AskLit Project Scaffolder")
    st.markdown(
        "Add a knowledge base, write a prompt, try the assistant, and measure it "
        "against gold-labeled scenarios. Export a deployable app when you are "
        "satisfied with the results."
    )

    if "scaffold_id" not in st.session_state:
        initialize_scaffold_storage()
    if "app_config" not in st.session_state:
        st.session_state.app_config = default_scaffold_config()
    ensure_model_defaults(st.session_state.app_config)

    render_workspace_controls()
    access.render_access_status()

    step_key = render_step_selector()
    if render_step(step_key):
        render_next_step_button(step_key)


if __name__ == "__main__":
    main()
