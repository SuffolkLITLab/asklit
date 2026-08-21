import os
import re

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import chromadb
from asklit.embeddings import LiteLLMEmbeddingFunction
from asklit.db import get_connection
from asklit.config import get_setting

CHROMA_PATH_DEFAULT = os.path.join("data", "chroma")


def get_chroma_path():
    return os.environ.get("CHROMA_PATH", CHROMA_PATH_DEFAULT)


def get_chroma_client(chroma_path=None):
    if chroma_path is None:
        chroma_path = get_chroma_path()
    return chromadb.PersistentClient(path=chroma_path)


COLLECTION_NAME = "asklit_docs"
DEFAULT_KNOWLEDGEBASE = "default"
STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "them",
    "these",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "you",
    "your",
}


def get_collection(chroma_path=None):
    client = get_chroma_client(chroma_path=chroma_path)
    embedding_function = LiteLLMEmbeddingFunction()
    return client.get_or_create_collection(
        name=COLLECTION_NAME, embedding_function=embedding_function
    )


def add_document_to_index(
    document_id, chunks, chroma_path=None, knowledgebase=DEFAULT_KNOWLEDGEBASE
):
    collection = get_collection(chroma_path=chroma_path)
    # ...

    ids = [f"{document_id}_{chunk['chunk_index']}" for chunk in chunks]
    documents = [chunk["content"] for chunk in chunks]
    metadatas = [
        {
            "document_id": document_id,
            "knowledgebase": knowledgebase or DEFAULT_KNOWLEDGEBASE,
            "page_number": chunk["page_number"],
            "chunk_index": chunk["chunk_index"],
        }
        for chunk in chunks
    ]

    collection.add(ids=ids, documents=documents, metadatas=metadatas)


def delete_document_from_index(document_id):
    collection = get_collection()
    collection.delete(where={"document_id": document_id})


def resolve_document_filter(knowledgebase=None, connected_files=None, db_path=None):
    knowledgebase = knowledgebase or DEFAULT_KNOWLEDGEBASE
    connected_files = connected_files or []

    conn = get_connection(db_path=db_path)
    cursor = conn.cursor()
    if connected_files:
        placeholders = ",".join("?" for _ in connected_files)
        cursor.execute(
            f"""
            SELECT id FROM documents
            WHERE status = 'indexed'
              AND knowledgebase = ?
              AND filename IN ({placeholders})
            """,
            [knowledgebase, *connected_files],
        )
    else:
        cursor.execute(
            "SELECT id FROM documents WHERE status = 'indexed' AND knowledgebase = ?",
            (knowledgebase,),
        )
    document_ids = {row["id"] for row in cursor.fetchall()}
    conn.close()
    return document_ids


def query_keyword_index(
    query_text,
    n_results=3,
    knowledgebase=None,
    connected_files=None,
    db_path=None,
):
    terms = [
        term
        for term in re.findall(r"[a-zA-Z0-9]+", query_text.lower())
        if len(term) > 2 and term not in STOPWORDS
    ]
    if not terms:
        return []

    conn = get_connection(db_path=db_path)
    cursor = conn.cursor()
    params = [knowledgebase or DEFAULT_KNOWLEDGEBASE]
    files_clause = ""
    if connected_files:
        placeholders = ",".join("?" for _ in connected_files)
        files_clause = f"AND d.filename IN ({placeholders})"
        params.extend(connected_files)

    cursor.execute(
        f"""
        SELECT c.document_id, c.chunk_index, c.page_number, c.content
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.status = 'indexed' AND d.knowledgebase = ?
        {files_clause}
        """,
        params,
    )
    rows = cursor.fetchall()
    conn.close()

    scored = []
    for row in rows:
        content = row["content"]
        content_lower = content.lower()
        matched_terms = [term for term in terms if term in content_lower]
        if not matched_terms:
            continue

        score = sum(content_lower.count(term) for term in matched_terms)
        if len(matched_terms) > 1:
            score += len(matched_terms) * 2

        scored.append(
            {
                "content": content,
                "metadata": {
                    "document_id": row["document_id"],
                    "page_number": row["page_number"],
                    "chunk_index": row["chunk_index"],
                },
                "distance": None,
                "keyword_score": score,
                "matched_terms": matched_terms,
            }
        )

    scored.sort(key=lambda item: item["keyword_score"], reverse=True)
    return scored[:n_results]


def query_index(
    query_text,
    n_results=None,
    knowledgebase=None,
    connected_files=None,
    db_path=None,
    chroma_path=None,
):
    if n_results is None:
        n_results = int(get_setting("retrieval.top_k", 5))
    knowledgebase = knowledgebase or DEFAULT_KNOWLEDGEBASE
    connected_files = connected_files or []
    allowed_document_ids = resolve_document_filter(
        knowledgebase, connected_files, db_path=db_path
    )
    if not allowed_document_ids:
        return []

    collection = get_collection(chroma_path=chroma_path)
    vector_n_results = max(n_results * 6, 50)
    query_kwargs = {"query_texts": [query_text], "n_results": vector_n_results}
    if knowledgebase != DEFAULT_KNOWLEDGEBASE:
        query_kwargs["where"] = {"knowledgebase": knowledgebase}
    results = collection.query(**query_kwargs)

    # Format results
    vector_results = []
    if results["documents"]:
        for i in range(len(results["documents"][0])):
            metadata = results["metadatas"][0][i]
            if metadata.get("document_id") not in allowed_document_ids:
                continue
            vector_results.append(
                {
                    "content": results["documents"][0][i],
                    "metadata": metadata,
                    "distance": (
                        results["distances"][0][i] if "distances" in results else None
                    ),
                    "keyword_score": 0,
                    "matched_terms": [],
                }
            )

    keyword_results = query_keyword_index(
        query_text,
        n_results=min(3, n_results),
        knowledgebase=knowledgebase,
        connected_files=connected_files,
        db_path=db_path,
    )
    combined_results = []
    seen = set()

    for result in keyword_results + vector_results:
        metadata = result["metadata"]
        key = (metadata.get("document_id"), metadata.get("chunk_index"))
        if key in seen:
            continue
        seen.add(key)
        combined_results.append(result)
        if len(combined_results) >= n_results:
            break

    return combined_results
