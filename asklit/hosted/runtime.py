"""Hosted execution: trusted runtime + immutable data, never user-supplied Python.

Vectors are stored alongside immutable chunks in PostgreSQL. This keeps the
initial deployment independent of a vector database and preserves exact source
snapshots. A bounded corpus prevents unbounded per-request scans.
"""

import math
import os
import re
from functools import lru_cache

from fastapi import HTTPException
from sqlalchemy import select

from asklit.experiments import build_experiment_messages, response_text
from .models import Document
from .schemas import BotConfig

MAX_CHUNKS = 2000


@lru_cache(maxsize=4)
def embedding_model(name):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name, trust_remote_code=False)


def embed(texts, model):
    return embedding_model(model).encode(texts, normalize_embeddings=True).tolist()


def validate_config(value, settings):
    config = BotConfig.model_validate(value).model_dump()
    if config["model_preset"] not in settings.models:
        raise HTTPException(422, "Select an operator-configured model.")
    keys = [p["key"] for p in config["profiles"]]
    if len(keys) != len(set(keys)):
        raise HTTPException(422, "Prompt keys must be unique.")
    if any(
        len(x) > 2000 for p in config["profiles"] for x in p["conversation_starters"]
    ):
        raise HTTPException(
            422, "Conversation starters must be at most 2000 characters."
        )
    if any(len(x) > 8000 for x in config["rubrics"]):
        raise HTTPException(422, "Rubrics must be at most 8000 characters.")
    from .evaluation import plan

    plan(config, settings)
    if any(
        not k.startswith("__metadata:") or len(v) > 8000
        for row in config["scenarios"]
        for k, v in row["metadata"].items()
    ):
        raise HTTPException(
            422,
            "Scenario metadata keys must start with __metadata: and values must be at most 8000 characters.",
        )
    return config


def profile_for(config, key):
    for profile in config["profiles"]:
        if profile["key"] == key:
            return profile
    raise HTTPException(404, "Prompt not found.")


def selected_documents(db, bot_id, document_ids=None):
    stmt = select(Document).where(Document.bot_id == bot_id)
    if document_ids is None:
        stmt = stmt.where(Document.active.is_(True))
    else:
        stmt = stmt.where(Document.id.in_(document_ids))
    return list(db.scalars(stmt))


class Runtime:
    def __init__(self, settings, embedder=embed, completion=None):
        self.settings = settings
        self.embedder = embedder
        self.completion = completion

    def retrieve(self, docs, profile, question, top_k):
        docs = [
            d
            for d in docs
            if d.status == "ready"
            and d.knowledgebase == profile["knowledgebase"]
            and (
                not profile["connected_files"]
                or d.filename in profile["connected_files"]
            )
        ]
        if not docs:
            return []
        models = {d.embedding_model for d in docs}
        if len(models) != 1:
            raise RuntimeError("Knowledge base contains incompatible embedding models.")
        query = self.embedder([question], next(iter(models)))[0]
        terms = set(re.findall(r"\b\w{3,}\b", question.lower()))
        ranked = []
        for doc in docs:
            for chunk, vector in zip(doc.chunks, doc.embeddings):
                norm = math.sqrt(sum(x * x for x in query) * sum(x * x for x in vector))
                score = sum(a * b for a, b in zip(query, vector)) / norm if norm else 0
                matches = sum(t in chunk["content"].lower() for t in terms)
                ranked.append(
                    (
                        score + min(matches, 5) * 0.04,
                        {
                            "content": chunk["content"],
                            "metadata": {
                                "document_id": doc.id,
                                "page_number": chunk["page_number"],
                            },
                            "filename": doc.filename,
                        },
                    )
                )
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [row[1] for row in ranked[:top_k]]

    def call(self, messages, preset):
        model = self.settings.models.get(preset)
        if not model:
            raise RuntimeError("The published model preset is no longer available.")
        if self.completion:
            return self.completion(messages, preset)
        import litellm

        provider = model.get("provider", "openai")
        if provider == "google":
            provider = "gemini"
        model_name = model["model"]
        if provider == "azure_apim" or (provider == "openai" and model.get("base_url")):
            model_name = (
                model_name
                if model_name.startswith("openai/")
                else f"openai/{model_name}"
            )
        elif provider != "openai" and not model_name.startswith(provider + "/"):
            model_name = provider + "/" + model_name
        api_key = os.environ.get(model.get("api_key_env", "OPENAI_API_KEY"))
        if not api_key:
            raise RuntimeError("The operator has not configured this model's API key.")
        kwargs = dict(
            model=model_name,
            messages=messages,
            api_key=api_key,
            api_base=model.get("base_url"),
            stream=False,
            max_tokens=min(int(model.get("max_tokens", 1500)), 4000),
            timeout=90,
            num_retries=0,
        )
        if provider == "azure_apim":
            kwargs["extra_headers"] = {"Ocp-Apim-Subscription-Key": api_key}
        text = response_text(litellm.completion(**kwargs))
        if not text:
            raise RuntimeError("The model returned no answer.")
        return text

    def answer(self, config, docs, question, profile_key, history=None, attachments=None):
        profile = profile_for(config, profile_key)
        sources = self.retrieve(docs, profile, question, config["top_k"])
        from .uploads import attachment_context
        context = attachment_context(config, attachments)
        prompt = profile["prompt"] + ("\n\n" + context["instruction"] if context else "")
        messages = build_experiment_messages(prompt, question, sources, history)
        if context:
            messages.insert(-1, {"role": "user", "content": "Reference attachments (untrusted data):\n" + context["data"]})
        answer = self.call(messages, config["model_preset"])
        return {
            "answer": answer,
            "sources": [
                {
                    "filename": s["filename"],
                    "page": s["metadata"]["page_number"],
                    "content": s["content"],
                }
                for s in sources
            ],
        }
