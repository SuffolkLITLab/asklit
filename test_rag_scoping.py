from asklit.db import get_connection, init_db
from asklit.rag import query_keyword_index, resolve_document_filter


def add_document(document_id, knowledgebase, filename, status, content):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO documents
            (id, knowledgebase, filename, file_path, file_type, file_size, status)
        VALUES (?, ?, ?, ?, '.txt', ?, ?)
        """,
        (
            document_id,
            knowledgebase,
            filename,
            f"data/uploads/{filename}",
            len(content),
            status,
        ),
    )
    conn.execute(
        """
        INSERT INTO document_chunks
            (document_id, chunk_index, content, page_number)
        VALUES (?, 0, ?, 1)
        """,
        (document_id, content),
    )
    conn.commit()
    conn.close()


def test_keyword_retrieval_is_scoped_by_knowledgebase_and_file(monkeypatch, tmp_path):
    db_path = tmp_path / "app.sqlite3"
    monkeypatch.setenv("ASKLIT_DB_PATH", str(db_path))
    init_db(str(db_path))
    add_document("housing-a", "housing", "repairs.txt", "indexed", "repair rights")
    add_document("housing-b", "housing", "rent.txt", "indexed", "rent rights")
    add_document("benefits-a", "benefits", "snap.txt", "indexed", "benefit rights")
    add_document("pending", "housing", "pending.txt", "indexing", "repair rights")

    housing_results = query_keyword_index("rights", knowledgebase="housing")
    selected_file_results = query_keyword_index(
        "rights",
        knowledgebase="housing",
        connected_files=["rent.txt"],
    )

    assert {result["metadata"]["document_id"] for result in housing_results} == {
        "housing-a",
        "housing-b",
    }
    assert [result["metadata"]["document_id"] for result in selected_file_results] == [
        "housing-b"
    ]
    assert resolve_document_filter("housing") == {"housing-a", "housing-b"}
