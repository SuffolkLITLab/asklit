import os
import sys
from types import SimpleNamespace

sys.modules.setdefault("litellm", SimpleNamespace())

from asklit.db import init_db
from asklit.scaffold import knowledge


class FakeUpload:
    def __init__(self, name, text):
        self.name = name
        self._data = text.encode("utf-8")
        self.size = len(self._data)

    def getbuffer(self):
        return self._data


class FakeCollection:
    def __init__(self):
        self.records = {}

    def add(self, ids, documents, metadatas):
        for chunk_id, metadata in zip(ids, metadatas):
            self.records[chunk_id] = dict(metadata)

    def get(self, where):
        document_id = where["document_id"]
        ids = [
            chunk_id
            for chunk_id, metadata in self.records.items()
            if metadata["document_id"] == document_id
        ]
        return {"ids": ids, "metadatas": [self.records[chunk_id] for chunk_id in ids]}

    def update(self, ids, metadatas):
        for chunk_id, metadata in zip(ids, metadatas):
            self.records[chunk_id] = dict(metadata)


def setup_session(tmp_path, monkeypatch):
    db_path = str(tmp_path / "app.sqlite3")
    init_db(db_path)
    collection = FakeCollection()
    monkeypatch.setattr(
        knowledge,
        "add_document_to_index",
        lambda document_id, chunks, chroma_path=None, knowledgebase=None: collection.add(
            [f"{document_id}_{chunk['chunk_index']}" for chunk in chunks],
            [chunk["content"] for chunk in chunks],
            [
                {
                    "document_id": document_id,
                    "knowledgebase": knowledgebase,
                    "chunk_index": chunk["chunk_index"],
                }
                for chunk in chunks
            ],
        ),
    )
    monkeypatch.setattr(knowledge, "get_collection", lambda chroma_path=None: collection)
    return db_path, collection


def test_reprocessing_the_same_file_does_not_duplicate_it(tmp_path, monkeypatch):
    db_path, collection = setup_session(tmp_path, monkeypatch)
    uploads = str(tmp_path / "uploads")
    document = FakeUpload("guide.txt", "Tenants must receive fourteen days of notice. " * 40)

    first = knowledge.index_uploaded_documents(
        [document], "housing", db_path, str(tmp_path / "chroma"), uploads
    )
    second = knowledge.index_uploaded_documents(
        [document], "housing", db_path, str(tmp_path / "chroma"), uploads
    )

    assert first["indexed"] == ["guide.txt"]
    assert second["indexed"] == []
    assert second["skipped"] == ["guide.txt"]
    assert len(knowledge.list_indexed_documents(db_path, "housing")) == 1
    assert len({metadata["document_id"] for metadata in collection.records.values()}) == 1
    # The rejected copy is not left behind on disk either.
    assert len(os.listdir(uploads)) == 1


def test_one_unreadable_file_does_not_lose_the_batch(tmp_path, monkeypatch):
    db_path, _collection = setup_session(tmp_path, monkeypatch)
    real_extract = knowledge.extract_text

    def flaky_extract(path):
        if path.endswith(".pdf"):
            raise ValueError("Encrypted PDF")
        return real_extract(path)

    monkeypatch.setattr(knowledge, "extract_text", flaky_extract)

    outcome = knowledge.index_uploaded_documents(
        [
            FakeUpload("broken.pdf", "unreadable"),
            FakeUpload("good.txt", "Fourteen days of notice is required. " * 40),
        ],
        "housing",
        db_path,
        str(tmp_path / "chroma"),
        str(tmp_path / "uploads"),
    )

    assert outcome["indexed"] == ["good.txt"]
    assert outcome["failed"][0][0] == "broken.pdf"


def test_renaming_a_knowledgebase_moves_its_documents(tmp_path, monkeypatch):
    db_path, collection = setup_session(tmp_path, monkeypatch)
    chroma_path = str(tmp_path / "chroma")
    knowledge.index_uploaded_documents(
        [FakeUpload("guide.txt", "Fourteen days of notice is required. " * 40)],
        "default",
        db_path,
        chroma_path,
        str(tmp_path / "uploads"),
    )

    moved = knowledge.rename_knowledgebase("default", "housing", db_path, chroma_path)

    assert moved == 1
    assert knowledge.knowledgebase_document_counts(db_path) == {"housing": 1}
    assert all(
        metadata["knowledgebase"] == "housing"
        for metadata in collection.records.values()
    )


def test_renaming_is_a_no_op_without_matching_documents(tmp_path, monkeypatch):
    db_path, _collection = setup_session(tmp_path, monkeypatch)

    assert knowledge.rename_knowledgebase("a", "b", db_path, str(tmp_path / "chroma")) == 0
    assert knowledge.rename_knowledgebase("a", "a", db_path, str(tmp_path / "chroma")) == 0
