"""Step 2: write the system prompt and pair it with a knowledge base."""

import streamlit as st

from asklit.scaffold.config import normalize_prompt_profiles, slugify_key
from asklit.scaffold.knowledge import (
    knowledgebase_document_counts,
    list_indexed_documents,
    rename_knowledgebase,
)
from asklit.scaffold.ui import session_paths


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
    db_path, chroma_path = session_paths()

    selected_index = st.selectbox(
        "Prompt to edit",
        range(len(profiles)),
        format_func=lambda index: profiles[index]["label"],
        key="prompt_editor_index",
        help=(
            "Each prompt becomes its own choice in the deployed chat's sidebar, "
            "with its own knowledge base."
        ),
    )
    profile = profiles[selected_index]
    profile["label"] = st.text_input("Prompt name", profile["label"])
    profile["knowledgebase"] = _render_knowledgebase_field(
        profile, db_path, chroma_path
    )
    profile["prompt"] = st.text_area(
        "System prompt",
        profile["prompt"],
        height=280,
        help="Describe the assistant's role, audience, boundaries, and desired answer style.",
    )
    profile["conversation_starters"] = [
        line.strip()
        for line in st.text_area(
            "Conversation starters",
            value="\n".join(profile.get("conversation_starters", [])),
            height=120,
            help="Add one example question per line. These appear as quick-start buttons in Chat.",
        ).splitlines()
        if line.strip()
    ]
    _render_advanced_pairing(profile, selected_index, db_path)
    profile["key"] = slugify_key(profile["label"], profile["key"])

    action_columns = st.columns(2)
    if action_columns[0].button("Add another prompt"):
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
        st.rerun()
    if len(profiles) > 1 and action_columns[1].button("Remove this prompt"):
        profiles.pop(selected_index)
        st.rerun()

    st.info(
        "Next, try the advisor in **3. Chat**, then build repeatable scenarios in "
        "**4. Evaluate**."
    )


def _render_knowledgebase_field(profile, db_path, chroma_path):
    """Rename the knowledge base without orphaning the documents inside it."""
    previous = profile["knowledgebase"]
    knowledgebase = slugify_key(
        st.text_input(
            "Knowledge-base name",
            previous,
            help="Prompts with the same knowledge-base name search the same uploaded sources.",
        ),
        previous,
    )
    if knowledgebase != previous:
        # Without this, retrieval silently returns nothing after a rename and the
        # model answers from general knowledge instead.
        moved = rename_knowledgebase(previous, knowledgebase, db_path, chroma_path)
        if moved:
            st.info(
                f"Moved {moved} indexed document(s) from `{previous}` to "
                f"`{knowledgebase}`."
            )

    indexed_count = knowledgebase_document_counts(db_path).get(knowledgebase, 0)
    if indexed_count:
        st.caption(f"`{knowledgebase}` currently holds {indexed_count} document(s).")
    else:
        st.warning(
            f"No indexed documents belong to `{knowledgebase}` yet. Upload some in "
            "**1. Knowledge**, or this prompt will answer without any sources."
        )
    return knowledgebase


def _render_advanced_pairing(profile, index, db_path):
    """Expose the deployment-level pairing fields without cluttering the step."""
    with st.expander("Advanced: deployment details for this prompt", expanded=False):
        profile["key"] = slugify_key(
            st.text_input(
                "YAML key",
                profile["key"],
                key=f"prompt_key_{index}",
                help="Used for the prompt's YAML filename and admin overrides.",
            ),
            profile["key"],
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

        configured = profile.get("connected_files") or []
        selected = st.multiselect(
            "Connected files",
            available,
            default=[name for name in configured if name in available] or available,
            key=f"prompt_files_{index}",
            help=(
                "Leave every file selected to search the whole knowledge base, "
                "including anything you upload later. Narrow it to restrict this "
                "prompt to specific documents."
            ),
        )
        # An empty list means "every file in this knowledge base", so selecting
        # them all is stored that way and later uploads are included too.
        profile["connected_files"] = (
            [] if not selected or set(selected) == set(available) else selected
        )
        if not profile["connected_files"]:
            st.caption(
                f"Searching every document in `{profile['knowledgebase']}`, "
                "including ones added later."
            )
