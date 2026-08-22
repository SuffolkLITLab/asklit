"""Knowledge-base indexing for the scaffolder's isolated session storage.

Every helper takes explicit database and Chroma paths because each browser
session gets its own UUID-named storage.
"""

import os
import uuid

from asklit.db import get_connection
from asklit.ingestion import chunk_pages, extract_text, get_content_hash
from asklit.rag import add_document_to_index, get_collection


def list_indexed_documents(db_path, knowledgebase=None):
    """Return indexed documents, optionally for a single knowledge base."""
    conn = get_connection(db_path=db_path)
    if knowledgebase:
        rows = conn.execute(
            "SELECT id, knowledgebase, filename, file_size, content_hash "
            "FROM documents WHERE knowledgebase = ? ORDER BY filename",
            (knowledgebase,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, knowledgebase, filename, file_size, content_hash "
            "FROM documents ORDER BY knowledgebase, filename"
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_document_labels(db_path):
    """Return document labels for experiment citations."""
    conn = get_connection(db_path=db_path)
    rows = conn.execute("SELECT id, filename FROM documents").fetchall()
    conn.close()
    return {row["id"]: row["filename"] for row in rows}


def knowledgebase_document_counts(db_path):
    """Count indexed documents per knowledge base."""
    conn = get_connection(db_path=db_path)
    rows = conn.execute(
        "SELECT knowledgebase, COUNT(*) AS total FROM documents GROUP BY knowledgebase"
    ).fetchall()
    conn.close()
    return {row["knowledgebase"]: row["total"] for row in rows}


def get_knowledgebase_sample(db_path, knowledgebase, max_chars=12000):
    """Return a bounded source sample for generating grounded scenarios."""
    conn = get_connection(db_path=db_path)
    rows = conn.execute(
        """
        SELECT dc.content
        FROM document_chunks AS dc
        JOIN documents AS d ON d.id = dc.document_id
        WHERE d.knowledgebase = ?
        ORDER BY d.filename, dc.chunk_index
        LIMIT 30
        """,
        (knowledgebase,),
    ).fetchall()
    conn.close()
    sample = "\n\n".join(str(row["content"]) for row in rows)
    return sample[:max_chars]


def _existing_content_hashes(db_path, knowledgebase):
    conn = get_connection(db_path=db_path)
    rows = conn.execute(
        "SELECT content_hash FROM documents WHERE knowledgebase = ?",
        (knowledgebase,),
    ).fetchall()
    conn.close()
    return {row["content_hash"] for row in rows if row["content_hash"]}


def index_uploaded_documents(
    uploaded_files,
    knowledgebase,
    db_path,
    chroma_path,
    uploads_dir,
    progress=None,
):
    """Index uploaded files once each, reporting per-file outcomes.

    ``content_hash`` is checked before indexing, so pressing the process button
    twice cannot duplicate a document's chunks in the vector index. One bad file
    is reported and skipped instead of aborting the whole batch.
    """
    os.makedirs(uploads_dir, exist_ok=True)
    seen_hashes = _existing_content_hashes(db_path, knowledgebase)
    indexed = []
    skipped = []
    failed = []

    total = len(uploaded_files)
    for position, uploaded_file in enumerate(uploaded_files):
        if progress:
            progress(position, total, uploaded_file.name)
        file_id = str(uuid.uuid4())
        extension = os.path.splitext(uploaded_file.name)[1]
        file_path = os.path.join(uploads_dir, f"{file_id}{extension}")
        try:
            with open(file_path, "wb") as handle:
                handle.write(uploaded_file.getbuffer())

            full_text, pages = extract_text(file_path)
            content_hash = get_content_hash(full_text)
            if content_hash in seen_hashes:
                skipped.append(uploaded_file.name)
                os.remove(file_path)
                continue

            chunks = chunk_pages(pages)
            if not chunks:
                failed.append((uploaded_file.name, "No readable text was found."))
                os.remove(file_path)
                continue

            conn = get_connection(db_path=db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO documents (id, knowledgebase, filename, file_path, "
                "file_type, file_size, content_hash, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    knowledgebase,
                    uploaded_file.name,
                    f"data/uploads/{file_id}{extension}",
                    extension,
                    uploaded_file.size,
                    content_hash,
                    "indexed",
                ),
            )
            cursor.executemany(
                "INSERT INTO document_chunks (document_id, chunk_index, content, "
                "page_number) VALUES (?, ?, ?, ?)",
                [
                    (file_id, chunk["chunk_index"], chunk["content"], chunk["page_number"])
                    for chunk in chunks
                ],
            )
            conn.commit()
            conn.close()

            add_document_to_index(
                file_id,
                chunks,
                chroma_path=chroma_path,
                knowledgebase=knowledgebase,
            )
            seen_hashes.add(content_hash)
            indexed.append(uploaded_file.name)
        except Exception as exc:  # one unreadable file must not lose the batch
            failed.append((uploaded_file.name, str(exc)))

    if progress:
        progress(total, total, "")
    return {"indexed": indexed, "skipped": skipped, "failed": failed}


def rename_knowledgebase(old_name, new_name, db_path, chroma_path):
    """Move indexed documents when a prompt's knowledge-base name changes.

    Without this, renaming the knowledge base after uploading silently orphans
    every document: retrieval returns nothing and the model answers from general
    knowledge instead, which is the hardest RAG failure for a beginner to spot.
    """
    if not old_name or not new_name or old_name == new_name:
        return 0

    conn = get_connection(db_path=db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM documents WHERE knowledgebase = ?",
        (old_name,),
    )
    document_ids = [row["id"] for row in cursor.fetchall()]
    if not document_ids:
        conn.close()
        return 0
    cursor.execute(
        "UPDATE documents SET knowledgebase = ? WHERE knowledgebase = ?",
        (new_name, old_name),
    )
    conn.commit()
    conn.close()

    collection = get_collection(chroma_path=chroma_path)
    for document_id in document_ids:
        existing = collection.get(where={"document_id": document_id})
        ids = existing.get("ids") or []
        if not ids:
            continue
        collection.update(
            ids=ids,
            metadatas=[
                {**metadata, "knowledgebase": new_name}
                for metadata in existing.get("metadatas") or []
            ],
        )
    return len(document_ids)
