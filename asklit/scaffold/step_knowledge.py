"""Step 1: upload documents and index them into a knowledge base."""

import os

import streamlit as st

from asklit.scaffold.config import normalize_prompt_profiles
from asklit.scaffold.knowledge import index_uploaded_documents, list_indexed_documents
from asklit.scaffold.ui import session_paths


def render_knowledge_step():
    """Upload, index, and review the documents each knowledge base holds."""
    config = st.session_state.app_config
    db_path, chroma_path = session_paths()
    st.header("Add a knowledge base")
    st.write("Upload the PDFs or documents you want your AI to know about.")
    config["prompt_profiles"] = normalize_prompt_profiles(config.get("prompt_profiles"))
    profiles = config["prompt_profiles"]

    selected_index = st.selectbox(
        "Attach uploaded files to",
        range(len(profiles)),
        format_func=lambda index: (
            f"{profiles[index]['label']} ({profiles[index]['knowledgebase']})"
        ),
        key="knowledge_target_profile",
    )
    selected_profile = profiles[selected_index]
    knowledgebase = selected_profile["knowledgebase"]

    existing = list_indexed_documents(db_path, knowledgebase)
    if existing:
        st.caption(f"Already indexed in `{knowledgebase}`:")
        for document in existing:
            st.markdown(
                f"- {document['filename']} "
                f"({round(document['file_size'] / 1024)} KB)"
            )
    else:
        st.caption(f"`{knowledgebase}` has no indexed documents yet.")

    uploader_key = (
        f"knowledge_upload_{st.session_state.get('knowledge_upload_version', 0)}"
    )
    uploaded_files = st.file_uploader(
        "Upload Documents",
        accept_multiple_files=True,
        type=["pdf", "docx", "txt", "md"],
        key=uploader_key,
    )

    if not uploaded_files or not st.button("Process & Index Documents"):
        return

    progress = st.progress(0.0, text="Preparing documents…")

    def report(position, total, filename):
        progress.progress(
            min(position / total, 1.0) if total else 1.0,
            text=f"Indexing {filename}…" if filename else "Finishing…",
        )

    with st.spinner("Chunking and Embedding... (this may take a minute)"):
        outcome = index_uploaded_documents(
            uploaded_files,
            knowledgebase,
            db_path,
            chroma_path,
            os.path.join(st.session_state.temp_data_dir, "uploads"),
            progress=report,
        )

    # An empty connected_files list means "every document in this knowledge
    # base", so newly indexed files are only listed when the prompt has already
    # been narrowed to specific documents.
    if selected_profile["connected_files"]:
        for filename in outcome["indexed"]:
            if filename not in selected_profile["connected_files"]:
                selected_profile["connected_files"].append(filename)

    if outcome["indexed"]:
        st.success(f"Indexed {len(outcome['indexed'])} document(s).")
    if outcome["skipped"]:
        st.info(
            "Already in this knowledge base, so not indexed again: "
            + ", ".join(outcome["skipped"])
        )
    for filename, reason in outcome["failed"]:
        st.error(f"{filename} could not be indexed: {reason}")

    # Rotate the uploader key so the same files are not offered for reprocessing.
    st.session_state["knowledge_upload_version"] = (
        st.session_state.get("knowledge_upload_version", 0) + 1
    )
    st.rerun()
