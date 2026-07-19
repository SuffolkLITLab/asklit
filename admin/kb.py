import streamlit as st
import os
import uuid
import pandas as pd
from asklit.db import get_connection
from asklit.ingestion import extract_text, chunk_pages, get_content_hash
from asklit.rag import add_document_to_index, delete_document_from_index
from asklit.prompts import get_prompt_configs

# st.set_page_config(page_title="Knowledge Base", page_icon="📚")

UPLOAD_DIR = os.path.join("data", "uploads")


def main():
    st.title("📚 Knowledge Base")

    if not st.session_state.get("is_admin_authenticated"):
        st.error("Access denied. Please login.")
        st.stop()

    st.header("Upload Documents")
    prompt_configs = get_prompt_configs()
    knowledgebase_options = sorted(
        {config["knowledgebase"] for config in prompt_configs} | {"default"}
    )
    selected_knowledgebase = st.selectbox(
        "Knowledge Base",
        knowledgebase_options,
        help="Documents uploaded here are only searched by prompts connected to this knowledge base.",
    )
    uploaded_files = st.file_uploader(
        "Choose files",
        accept_multiple_files=True,
        type=["pdf", "docx", "txt", "md", "html"],
    )

    if uploaded_files:
        for uploaded_file in uploaded_files:
            if st.button(f"Process {uploaded_file.name}"):
                with st.spinner(f"Processing {uploaded_file.name}..."):
                    # Save to disk
                    file_id = str(uuid.uuid4())
                    ext = os.path.splitext(uploaded_file.name)[1]
                    os.makedirs(UPLOAD_DIR, exist_ok=True)
                    file_path = os.path.join(UPLOAD_DIR, f"{file_id}{ext}")

                    with open(file_path, "wb") as f:
                        f.write(uploaded_file.getbuffer())

                    # Extract and Chunk
                    try:
                        full_text, pages = extract_text(file_path)
                        content_hash = get_content_hash(full_text)
                        chunks = chunk_pages(pages)

                        # Save to SQLite - keep transaction short
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT INTO documents (id, knowledgebase, filename, file_path, file_type, file_size, content_hash, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                file_id,
                                selected_knowledgebase,
                                uploaded_file.name,
                                file_path,
                                ext,
                                uploaded_file.size,
                                content_hash,
                                "indexing",
                            ),
                        )

                        # Use executemany for efficiency
                        chunk_data = [
                            (
                                file_id,
                                chunk["chunk_index"],
                                chunk["content"],
                                chunk["page_number"],
                            )
                            for chunk in chunks
                        ]
                        cursor.executemany(
                            "INSERT INTO document_chunks (document_id, chunk_index, content, page_number) VALUES (?, ?, ?, ?)",
                            chunk_data,
                        )
                        conn.commit()
                        conn.close()

                        # Add to ChromaDB (Heavy lifting done outside main transaction)
                        add_document_to_index(
                            file_id, chunks, knowledgebase=selected_knowledgebase
                        )

                        # Update status to indexed
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE documents SET status = 'indexed' WHERE id = ?",
                            (file_id,),
                        )
                        conn.commit()
                        conn.close()

                        st.success(f"Indexed {uploaded_file.name} successfully!")
                    except Exception as e:
                        st.error(f"Error processing {uploaded_file.name}: {str(e)}")

    st.divider()
    st.header("Manage Documents")

    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, knowledgebase, filename, file_path, file_type, status, created_at FROM documents",
        conn,
    )
    conn.close()

    if not df.empty:
        if st.button(
            "🔄 Reindex All Documents",
            help="Rebuilds the vector index from the files currently in the repository.",
        ):
            with st.spinner("Reindexing everything..."):
                try:
                    # 1. Clear ChromaDB
                    from asklit.rag import get_chroma_client, COLLECTION_NAME

                    client = get_chroma_client()
                    try:
                        client.delete_collection(COLLECTION_NAME)
                    except Exception as e:
                        st.caption(f"No existing vector collection to clear: {e}")

                    # 2. Re-process every doc in SQLite
                    for _, row in df.iterrows():
                        full_text, pages = extract_text(row["file_path"])
                        chunks = chunk_pages(pages)

                        # Update chunks in SQLite
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute(
                            "DELETE FROM document_chunks WHERE document_id = ?",
                            (row["id"],),
                        )
                        for chunk in chunks:
                            cursor.execute(
                                "INSERT INTO document_chunks (document_id, chunk_index, content, page_number) VALUES (?, ?, ?, ?)",
                                (
                                    row["id"],
                                    chunk["chunk_index"],
                                    chunk["content"],
                                    chunk["page_number"],
                                ),
                            )

                        # Add to ChromaDB
                        add_document_to_index(
                            row["id"],
                            chunks,
                            knowledgebase=row.get("knowledgebase", "default"),
                        )
                        conn.commit()
                        conn.close()
                    st.success("Successfully reindexed all documents!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Reindexing failed: {str(e)}")

        for i, row in df.iterrows():
            col1, col2 = st.columns([4, 1])
            col1.write(
                f"**{row['filename']}** ({row['file_type']}) - {row['knowledgebase']} - {row['created_at']}"
            )
            if col2.button("Delete", key=row["id"]):
                with st.spinner("Deleting..."):
                    # Delete from SQLite
                    conn = get_connection()
                    cursor = conn.cursor()

                    # Get file path before deleting record
                    cursor.execute(
                        "SELECT file_path FROM documents WHERE id = ?", (row["id"],)
                    )
                    file_res = cursor.fetchone()

                    # Delete child chunks first (though CASCADE should handle it, explicit is safer)
                    cursor.execute(
                        "DELETE FROM document_chunks WHERE document_id = ?",
                        (row["id"],),
                    )
                    cursor.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
                    conn.commit()
                    conn.close()

                    # Delete from ChromaDB
                    delete_document_from_index(row["id"])

                    # Delete from physical disk
                    if file_res and os.path.exists(file_res["file_path"]):
                        try:
                            os.remove(file_res["file_path"])
                        except Exception as e:
                            st.warning(f"Could not delete physical file: {str(e)}")

                    st.rerun()
    else:
        st.info("No documents indexed yet.")


if __name__ == "__main__":
    main()
