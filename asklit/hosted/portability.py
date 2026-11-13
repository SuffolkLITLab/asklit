"""Adapt hosted data to the established workspace and standalone bundle formats."""

import shutil
import tempfile
from pathlib import Path

from .runtime import validate_config
from .schemas import BotConfig
from .storage import object_store
from .evaluation import scaffold_scenarios, scenario_from_scaffold


def scaffold_config(config, settings, docs):
    # Imported lazily so merely starting the hosted API does not load Streamlit/Chroma.
    from asklit.scaffold.config import default_scaffold_config

    value = default_scaffold_config()
    value["app"].update(title=config["title"], welcome_message=config["welcome"], student_instructions=config.get("student_instructions", ""))
    value["branding"]["footer_text"] = config["footer"]
    preset = settings.models[config["model_preset"]]
    value["model"] = {
        "provider": preset.get("provider", "openai"),
        "name": preset["model"],
        "base_url": preset.get("base_url", ""),
        "use_local_embeddings": True,
        "local_embedding_model": docs[0].embedding_model
        if docs
        else settings.embedding_model,
    }
    value["retrieval"] = {"top_k": config["top_k"], "show_citations": config.get("show_citations", True)}
    value["prompt_profiles"] = [dict(p) for p in config["profiles"]]
    for p in value["prompt_profiles"]:
        if not p["connected_files"]:
            p["connected_files"] = [
                d.filename for d in docs if d.knowledgebase == p["knowledgebase"]
            ]
    return value


def workspace_yaml(config, settings, docs):
    from asklit.scaffold.workspace import export_workspace_yaml

    source = scaffold_config(config, settings, docs)
    source["hosted_evaluation"] = config.get("evaluation", {})
    source["hosted_presentation"] = {"show_citations": config.get("show_citations", True), "student_instructions": config.get("student_instructions", "")}
    return export_workspace_yaml(
        source,
        scaffold_scenarios(config["scenarios"]),
        config["rubrics"],
    )


def read_workspace(value, settings):
    from asklit.scaffold.workspace import import_workspace_yaml

    imported = import_workspace_yaml(value)
    source = imported["app_config"]
    config = BotConfig(
        show_citations=source.get("hosted_presentation", {}).get("show_citations", True),
        student_instructions=source.get("hosted_presentation", {}).get("student_instructions", ""),
        title=source.get("app", {}).get("title", "Imported AskLit"),
        welcome=source.get("app", {}).get("welcome_message", "How can I help you?"),
        model_preset=next(iter(settings.models)),
        profiles=source["prompt_profiles"],
        scenarios=[scenario_from_scaffold(s) for s in imported["evaluation_scenarios"]],
        rubrics=imported["evaluation_rubrics"],
        evaluation=source.get("hosted_evaluation", {}),
        footer=source.get("branding", {}).get("footer_text", "Made with AskLit"),
        top_k=source.get("retrieval", {}).get("top_k", 5),
    ).model_dump()
    return validate_config(config, settings), imported["source_files_to_reupload"]


def build_export(config, docs, settings, destination):
    """Write a fresh app database/index; never export hosted users or chat logs."""
    import chromadb
    from asklit.db import init_db, get_connection
    from asklit.scaffold.bundle import create_bundle, zip_directory

    bundle = None
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "data"
        data.mkdir()
        (data / "uploads").mkdir()
        database = str(data / "app.sqlite3")
        init_db(database)
        client = chromadb.PersistentClient(path=str(data / "chroma"))
        from asklit.embeddings import LiteLLMEmbeddingFunction

        collection = client.get_or_create_collection(
            "asklit_docs", embedding_function=LiteLLMEmbeddingFunction()
        )
        connection = get_connection(database)
        try:
            for doc in docs:
                filename = f"{doc.id}{Path(doc.filename).suffix.lower()}"
                with object_store(settings).materialize(doc.storage_key) as source:
                    shutil.copy2(source, data / "uploads" / filename)
                connection.execute(
                    "INSERT INTO documents (id, knowledgebase, filename, file_path, file_type, file_size, content_hash, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc.id,
                        doc.knowledgebase,
                        doc.filename,
                        f"data/uploads/{filename}",
                        Path(doc.filename).suffix,
                        doc.size,
                        doc.content_hash,
                        "indexed",
                    ),
                )
                connection.executemany(
                    "INSERT INTO document_chunks (document_id, chunk_index, page_number, content) VALUES (?, ?, ?, ?)",
                    [
                        (doc.id, c["chunk_index"], c["page_number"], c["content"])
                        for c in doc.chunks
                    ],
                )
                collection.add(
                    ids=[f"{doc.id}_{c['chunk_index']}" for c in doc.chunks],
                    embeddings=doc.embeddings,
                    documents=[c["content"] for c in doc.chunks],
                    metadatas=[
                        {
                            "document_id": doc.id,
                            "knowledgebase": doc.knowledgebase,
                            "page_number": c["page_number"],
                            "chunk_index": c["chunk_index"],
                        }
                        for c in doc.chunks
                    ],
                )
            connection.commit()
        finally:
            connection.close()
        try:
            bundle = create_bundle(scaffold_config(config, settings, docs), str(data))
            zip_directory(bundle, str(destination))
        finally:
            if bundle:
                shutil.rmtree(bundle)
