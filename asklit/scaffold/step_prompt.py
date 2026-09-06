"""Step 2: write the system prompt and pair it with a knowledge base."""

import streamlit as st

from asklit.scaffold.config import normalize_prompt_profiles, slugify_key
from asklit.scaffold.knowledge import (
    knowledgebase_document_counts,
    list_indexed_documents,
    rename_knowledgebase,
)
from asklit.scaffold.ui import session_paths

# Every editor field carries an explicit key so its in-progress text lives at a
# stable address in session_state. A keyless widget's id is a hash that includes
# its current value, so the id changes on every edit and a half-committed change
# has nowhere to survive: clicking a button while a text area still holds focus
# races the blur that commits the text, and the edit is dropped.
# See https://github.com/streamlit/streamlit/issues/8725. With a key plus
# on_change, the callback runs before the rerun's script body, so the text
# reaches app_config even when the click that triggered it is swallowed.
EDITOR_KEY_PREFIXES = (
    "prompt_label_",
    "prompt_kb_",
    "prompt_text_",
    "prompt_starters_",
    "prompt_key_",
    "prompt_files_",
)
KB_RENAME_NOTICE_KEY = "prompt_kb_rename_notice"
PROMPT_INDEX_KEY = "prompt_editor_index"


def render_prompt_step():
    """Edit one prompt at a time, with the advanced pairing fields tucked away."""
    st.header("Write a prompt")
    st.write(
        "A prompt tells the assistant how to behave. The knowledge-base name tells "
        "AskLit which uploaded sources this prompt may search. Nothing is published "
        "unless you later choose Export."
    )
    profiles = normalize_prompt_profiles(
        st.session_state.app_config.get("prompt_profiles")
    )
    st.session_state.app_config["prompt_profiles"] = profiles

    selected_index = st.selectbox(
        "Prompt to edit",
        range(len(profiles)),
        format_func=lambda index: profiles[index]["label"],
        key=PROMPT_INDEX_KEY,
        help=(
            "Each prompt becomes its own choice in the deployed chat's sidebar, "
            "with its own knowledge base."
        ),
    )
    render_profile_editor(selected_index)

    action_columns = st.columns(2)
    if action_columns[0].button("Add another prompt"):
        profile = profiles[selected_index]
        next_number = len(profiles) + 1
        profiles.append(
            {
                "key": f"prompt-{next_number}",
                "label": f"Prompt {next_number}",
                "knowledgebase": profile["knowledgebase"],
                "prompt": profile["prompt"],
                "conversation_starters": [],
                "connected_files": [],
            }
        )
        clear_editor_widget_state()
        st.rerun()
    if len(profiles) > 1 and action_columns[1].button("Remove this prompt"):
        profiles.pop(selected_index)
        clear_editor_widget_state()
        st.rerun()

    st.info(
        "Next, try the advisor in **3. Chat**, then build repeatable scenarios in "
        "**4. Evaluate**."
    )


@st.fragment
def render_profile_editor(index):
    """Render the fields for one prompt.

    This is a fragment so committing a field re-runs only the editor. As a
    full-page rerun it re-queried SQLite for document counts on every blur,
    which widened the window in which a click on a button elsewhere on the
    page was swallowed.
    """
    profile = editing_profile(index)
    if profile is None:
        return
    db_path, chroma_path = session_paths()

    label_key = f"prompt_label_{index}"
    seed_widget_state(label_key, profile["label"])
    st.text_input(
        "Prompt name",
        key=label_key,
        on_change=_sync_label,
        args=(index, label_key),
    )
    _render_knowledgebase_field(profile, index, db_path, chroma_path)
    prompt_key = f"prompt_text_{index}"
    seed_widget_state(prompt_key, profile["prompt"])
    st.text_area(
        "System prompt",
        key=prompt_key,
        height=280,
        on_change=sync_prompt_text,
        args=(index, prompt_key),
        help="Describe the assistant's role, audience, boundaries, and desired answer style.",
    )
    starters_key = f"prompt_starters_{index}"
    seed_widget_state(starters_key, "\n".join(profile.get("conversation_starters", [])))
    st.text_area(
        "Conversation starters",
        key=starters_key,
        height=120,
        on_change=sync_conversation_starters,
        args=(index, starters_key),
        help="Add one example question per line. These appear as quick-start buttons in Chat.",
    )
    _render_save_control(index, label_key, prompt_key, starters_key)
    _render_advanced_pairing(profile, index, db_path)


def _render_save_control(index, label_key, prompt_key, starters_key):
    """Give the autosave a visible, tappable commit point.

    Tapping this re-reads every field from session_state and writes it into the
    workspace, so a learner who cannot press Ctrl/Cmd+Enter — an iPad has no
    Command key — has an affordance that confirms the prompt is stored rather
    than having to trust an invisible save.
    """
    st.caption(
        "Your edits save as soon as you tap or click outside a box. You do not "
        "need to press Ctrl+Enter."
    )
    if st.button("Save prompt", key=f"prompt_save_{index}"):
        _sync_label(index, label_key)
        sync_prompt_text(index, prompt_key)
        sync_conversation_starters(index, starters_key)
        st.success("Prompt saved.")


def editing_profile(index):
    """Return the profile a callback is editing, or None if it has gone away.

    Callbacks fire before the script body, so the profile list can be shorter
    than it was when the widget was drawn.
    """
    profiles = st.session_state.app_config.get("prompt_profiles") or []
    return profiles[index] if 0 <= index < len(profiles) else None


def seed_widget_state(widget_key, value):
    """Give a keyed widget its starting text without fighting session_state.

    Passing ``value=`` alongside ``key=`` makes Streamlit warn once the key
    exists, so the stored value is the only default the widget ever sees.
    """
    if widget_key not in st.session_state:
        st.session_state[widget_key] = value


def sync_prompt_text(index, widget_key):
    """Commit the system prompt to the workspace config."""
    profile = editing_profile(index)
    if profile is not None:
        profile["prompt"] = st.session_state[widget_key]


def sync_conversation_starters(index, widget_key):
    """Commit the conversation starters, one per non-blank line."""
    profile = editing_profile(index)
    if profile is not None:
        profile["conversation_starters"] = [
            line.strip()
            for line in st.session_state[widget_key].splitlines()
            if line.strip()
        ]


def _sync_label(index, widget_key):
    """Commit the prompt name and keep its derived YAML key in step."""
    profile = editing_profile(index)
    if profile is None:
        return
    label = str(st.session_state[widget_key]).strip()
    if not label:
        # A blank name slugifies to nothing, which would orphan the YAML key.
        st.session_state[widget_key] = profile["label"]
        return
    profile["label"] = label
    profile["key"] = slugify_key(label, profile["key"])
    st.session_state[f"prompt_key_{index}"] = profile["key"]


def _sync_knowledgebase(index, widget_key, db_path, chroma_path):
    """Commit the knowledge-base name, moving its documents when it changes."""
    profile = editing_profile(index)
    if profile is None:
        return
    previous = profile["knowledgebase"]
    knowledgebase = slugify_key(st.session_state[widget_key], previous)
    # Show the slug the rest of the app will actually use, not the raw typing.
    st.session_state[widget_key] = knowledgebase
    if knowledgebase == previous:
        return
    profile["knowledgebase"] = knowledgebase
    # Without this, retrieval silently returns nothing after a rename and the
    # model answers from general knowledge instead.
    moved = rename_knowledgebase(previous, knowledgebase, db_path, chroma_path)
    if moved:
        # Callbacks cannot draw, so the message waits for the rerun they cause.
        st.session_state[KB_RENAME_NOTICE_KEY] = (
            f"Moved {moved} indexed document(s) from `{previous}` to "
            f"`{knowledgebase}`."
        )


def _sync_yaml_key(index, widget_key):
    """Commit a hand-edited YAML key, falling back to the existing slug."""
    profile = editing_profile(index)
    if profile is None:
        return
    profile["key"] = slugify_key(st.session_state[widget_key], profile["key"])
    st.session_state[widget_key] = profile["key"]


def _sync_connected_files(index, widget_key, available):
    """Store the file selection, treating 'everything' as an empty list.

    An empty list means "every file in this knowledge base", so selecting them
    all is stored that way and later uploads are included too.
    """
    profile = editing_profile(index)
    if profile is None:
        return
    selected = st.session_state[widget_key]
    profile["connected_files"] = (
        [] if not selected or set(selected) == set(available) else list(selected)
    )


def clear_editor_widget_state():
    """Drop the keyed editor widgets so they re-seed from the workspace config.

    The keys are index-based, so adding or removing a prompt would otherwise
    leave one profile's text showing under another profile's index.
    """
    stale = [
        state_key
        for state_key in st.session_state
        if state_key.startswith(EDITOR_KEY_PREFIXES)
    ]
    for state_key in stale:
        del st.session_state[state_key]


def _render_knowledgebase_field(profile, index, db_path, chroma_path):
    """Rename the knowledge base without orphaning the documents inside it."""
    widget_key = f"prompt_kb_{index}"
    seed_widget_state(widget_key, profile["knowledgebase"])
    st.text_input(
        "Knowledge-base name",
        key=widget_key,
        on_change=_sync_knowledgebase,
        args=(index, widget_key, db_path, chroma_path),
        help="Prompts with the same knowledge-base name search the same uploaded sources.",
    )
    rename_notice = st.session_state.pop(KB_RENAME_NOTICE_KEY, None)
    if rename_notice:
        st.info(rename_notice)

    knowledgebase = profile["knowledgebase"]
    indexed_count = knowledgebase_document_counts(db_path).get(knowledgebase, 0)
    if indexed_count:
        st.caption(f"`{knowledgebase}` currently holds {indexed_count} document(s).")
    else:
        st.warning(
            f"No indexed documents belong to `{knowledgebase}` yet. Upload some in "
            "**1. Knowledge**, or this prompt will answer without any sources."
        )


def _render_advanced_pairing(profile, index, db_path):
    """Expose the deployment-level pairing fields without cluttering the step."""
    with st.expander("Advanced: deployment details for this prompt", expanded=False):
        yaml_key = f"prompt_key_{index}"
        seed_widget_state(yaml_key, profile["key"])
        st.text_input(
            "YAML key",
            key=yaml_key,
            on_change=_sync_yaml_key,
            args=(index, yaml_key),
            help="Used for the prompt's YAML filename and admin overrides.",
        )
        available = [
            document["filename"]
            for document in list_indexed_documents(db_path, profile["knowledgebase"])
        ]
        if not available:
            profile["connected_files"] = []
            st.caption(
                "Upload documents in **1. Knowledge** to choose which ones this "
                "prompt may search."
            )
            return

        files_key = f"prompt_files_{index}"
        configured = profile.get("connected_files") or []
        seed_widget_state(
            files_key,
            [name for name in configured if name in available] or available,
        )
        st.multiselect(
            "Connected files",
            available,
            key=files_key,
            on_change=_sync_connected_files,
            args=(index, files_key, available),
            help=(
                "Leave every file selected to search the whole knowledge base, "
                "including anything you upload later. Narrow it to restrict this "
                "prompt to specific documents."
            ),
        )
        _sync_connected_files(index, files_key, available)
        if not profile["connected_files"]:
            st.caption(
                f"Searching every document in `{profile['knowledgebase']}`, "
                "including ones added later."
            )
