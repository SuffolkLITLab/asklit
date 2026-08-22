"""Shared scaffolder widgets: passwords, workspace files, and step navigation."""

import os
import tempfile
import uuid

import streamlit as st

from asklit.auth import hash_password
from asklit.db import init_db
from asklit.scaffold.config import default_scaffold_config, merge_workspace_config
from asklit.scaffold.workspace import export_workspace_yaml, import_workspace_yaml

# One workflow: learn with a knowledge base and a prompt, try it, measure it,
# then decide whether to ship it. Every deployment setting the exported app
# needs lives in the final step, so nobody has to configure branding or access
# control before knowing whether the assistant is any good.
STEPS = (
    ("knowledge", "1. Knowledge"),
    ("prompt", "2. Prompt"),
    ("chat", "3. Chat"),
    ("evaluate", "4. Evaluate"),
    ("export", "5. Export"),
)
STEP_STATE_KEY = "scaffold_step"
# Steps that spend the host's model credits and therefore sit behind the gate.
BILLED_STEPS = {"chat", "evaluate"}


def session_paths():
    """Return this browser session's isolated SQLite and Chroma paths."""
    return (
        os.path.join(st.session_state.temp_data_dir, "app.sqlite3"),
        os.path.join(st.session_state.temp_data_dir, "chroma"),
    )


def render_password_hash_setup(label, state_key, help_text):
    """Collect a password once, retain only its PBKDF2 hash, and show the result."""
    configured_hash = st.session_state.get(state_key)
    if configured_hash:
        st.success(f"{label} is configured.")
        st.caption(
            "Generated password hash (this will be placed in deployment secrets):"
        )
        st.code(configured_hash, language=None)
        if st.button(f"Change {label.lower()}", key=f"change_{state_key}"):
            st.session_state.pop(state_key, None)
            st.rerun()
        return configured_hash

    st.caption(help_text)
    with st.form(f"form_{state_key}", clear_on_submit=True):
        password = st.text_input(label, type="password", key=f"input_{state_key}")
        confirmation = st.text_input(
            f"Confirm {label.lower()}",
            type="password",
            key=f"confirm_{state_key}",
        )
        submitted = st.form_submit_button(f"Set {label.lower()}")
    if submitted:
        if len(password) < 8:
            st.error("Use at least 8 characters.")
        elif password != confirmation:
            st.error("The passwords do not match.")
        else:
            st.session_state[state_key] = hash_password(password)
            st.rerun()
    return None


def initialize_scaffold_storage():
    """Create isolated database/vector storage for the current browser session."""
    st.session_state.scaffold_id = str(uuid.uuid4())
    st.session_state.temp_data_dir = os.path.join(
        tempfile.gettempdir(), f"asklit_data_{st.session_state.scaffold_id}"
    )
    os.makedirs(st.session_state.temp_data_dir, exist_ok=True)
    init_db(os.path.join(st.session_state.temp_data_dir, "app.sqlite3"))


def render_workspace_controls():
    """Render YAML save/resume controls in the sidebar."""
    with st.sidebar.expander("Save or resume", expanded=False):
        st.caption(
            "Workspace YAML saves settings, prompts, and scenarios—not API keys, "
            "uploaded documents/images, or generated answers."
        )
        st.download_button(
            "Download workspace YAML",
            export_workspace_yaml(
                st.session_state.app_config,
                st.session_state.get("evaluation_scenarios", []),
                st.session_state.get("evaluation_rubrics", []),
            ),
            file_name="asklit-workspace.yml",
            mime="application/x-yaml",
        )
        uploaded_workspace = st.file_uploader(
            "Import workspace YAML",
            type=["yml", "yaml"],
            key="workspace_yaml_upload",
        )
        if st.button(
            "Import and replace current workspace",
            disabled=uploaded_workspace is None,
        ):
            _import_workspace(uploaded_workspace)

    if st.session_state.get("workspace_imported"):
        st.sidebar.success("Workspace imported.")
        source_files = st.session_state.get("workspace_source_files", [])
        if source_files:
            st.sidebar.warning(
                "Re-upload these knowledge-base files: " + ", ".join(source_files)
            )
        uploaded_assets = st.session_state.get("workspace_uploaded_assets", [])
        if uploaded_assets:
            st.sidebar.warning(
                "Re-upload these branding images in the Export step: "
                + ", ".join(uploaded_assets)
            )


def _import_workspace(uploaded_workspace):
    """Replace the session with an imported workspace."""
    try:
        imported = import_workspace_yaml(uploaded_workspace.getvalue())
    except (UnicodeDecodeError, ValueError) as exc:
        st.error(str(exc))
        return

    imported_config = merge_workspace_config(
        default_scaffold_config(), imported["app_config"]
    )
    scenarios = imported["evaluation_scenarios"]
    rubrics = imported.get("evaluation_rubrics", [])
    source_files = imported["source_files_to_reupload"]
    uploaded_assets = imported["uploaded_assets_to_reupload"]
    access_granted = st.session_state.get("scaffold_access_granted")
    st.session_state.clear()
    initialize_scaffold_storage()
    if access_granted:
        st.session_state["scaffold_access_granted"] = True
    st.session_state.app_config = imported_config
    st.session_state.evaluation_scenarios = scenarios
    st.session_state.evaluation_rubrics = rubrics
    st.session_state.workspace_source_files = source_files
    st.session_state.workspace_uploaded_assets = uploaded_assets
    st.session_state.workspace_imported = True
    st.rerun()


def render_step_selector():
    """Render the sidebar step list and return the selected step key."""
    label_to_key = {label: key for key, label in STEPS}
    st.session_state.setdefault(STEP_STATE_KEY, STEPS[0][1])
    selected_label = st.sidebar.radio(
        "Steps",
        [label for _key, label in STEPS],
        key=STEP_STATE_KEY,
        help="Steps can be visited in any order; nothing is published until Export.",
    )
    return label_to_key[selected_label]


def _set_step(step_label):
    st.session_state[STEP_STATE_KEY] = step_label


def render_next_step_button(step_key):
    """Offer an optional forward path while preserving the ability to skip steps."""
    step_keys = [key for key, _label in STEPS]
    if step_key not in step_keys or step_key == step_keys[-1]:
        return

    next_label = STEPS[step_keys.index(step_key) + 1][1]
    st.divider()
    st.button(
        f"Next: {next_label}",
        type="primary",
        key=f"next_{step_key}",
        on_click=_set_step,
        args=(next_label,),
    )
