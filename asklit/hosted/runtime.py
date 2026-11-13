"""Hosted execution: trusted runtime + immutable data, never user-supplied Python.

Vectors are stored alongside immutable chunks in PostgreSQL. This keeps the
initial deployment independent of a vector database and preserves exact source
snapshots. A bounded corpus prevents unbounded per-request scans.
"""

import math
import os
import re
import time
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
        # top_k is a ceiling, not a quota. Avoid weak matches and duplicate text.
        threshold = max(0.2, ranked[0][0] * 0.75) if ranked else 0.2
        selected, seen = [], set()
        for score, source in ranked:
            normalized = " ".join(source["content"].lower().split())
            if score < threshold or normalized in seen:continue
            seen.add(normalized);selected.append(source)
            if len(selected) >= top_k:break
        # Keep one context passage for short follow-ups with weak standalone matches.
        return selected or ([ranked[0][1]] if ranked else [])

    def call(self, messages, preset):
        from .usage import begin_call, finish_call
        model = self.settings.models.get(preset)
        if not model:
            raise RuntimeError("The published model preset is no longer available.")
        handle = begin_call(self.settings, preset, model, messages)
        started = time.perf_counter()
        usage = None
        try:
            if self.completion:
                answer = self.completion(messages, preset)
            elif model.get("provider") == "bedrock":
                answer, usage = self.bedrock(messages, model)
            else:
                answer, usage = self.litellm(messages, model)
            # Some models charge different rates for long context. Keep those calls
            # visible as unknown cost until the ledger supports multiple rate tiers.
            if usage and model.get("pricing_max_input_tokens") is not None:
                usage["unpriced_context"] = (usage.get("input_tokens") or 0) > model["pricing_max_input_tokens"]
            if not answer:
                raise RuntimeError("The model returned no answer.")
        except Exception:
            finish_call(handle, "failed", usage, time.perf_counter() - started)
            raise
        finish_call(handle, "succeeded", usage, time.perf_counter() - started)
        return answer

    def bedrock(self, messages, model):
        import boto3
        from botocore.config import Config
        # IAM credentials are supplied by the operator, never by an author.
        client = boto3.session.Session().client("bedrock-runtime", region_name=model.get("region", "us-east-1"),
            config=Config(connect_timeout=10, read_timeout=90, retries={"total_max_attempts": 1}))
        system = [{"text": m["content"]} for m in messages if m["role"] == "system" and m["content"].strip()]
        turns = []
        for message in messages:
            if message["role"] == "system" or not message["content"].strip(): continue
            if not turns and message["role"] == "assistant": continue
            content = {"text": message["content"]}
            if turns and turns[-1]["role"] == message["role"]:
                turns[-1]["content"].append(content)
            else:
                turns.append({"role": message["role"], "content": [content]})
        kwargs = {"modelId": model["model"], "messages": turns,
                  "inferenceConfig": {"maxTokens": min(int(model.get("max_tokens", 1500)), 4000)}}
        if system: kwargs["system"] = system
        if model.get("additional_model_request_fields"):
            kwargs["additionalModelRequestFields"] = model["additional_model_request_fields"]
        from botocore.exceptions import ClientError
        from .budget import ProviderAccessDenied, ProviderQuotaExceeded
        try:
            result = client.converse(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "AccessDeniedException":
                raise ProviderAccessDenied() from None
            if code == "ServiceQuotaExceededException":
                raise ProviderQuotaExceeded() from None
            raise
        answer = "".join(block.get("text", "") for block in result["output"]["message"]["content"])
        measured = result.get("usage", {})
        return answer, {"input_tokens": measured.get("inputTokens"), "output_tokens": measured.get("outputTokens"),
                        "cached_tokens": measured.get("cacheReadInputTokens", 0) + measured.get("cacheWriteInputTokens", 0)}

    def litellm(self, messages, model):
        import litellm
        provider = model.get("provider", "openai")
        if provider == "google": provider = "gemini"
        model_name = model["model"]
        if provider == "azure_apim" or (provider == "openai" and model.get("base_url")):
            model_name = model_name if model_name.startswith("openai/") else f"openai/{model_name}"
        elif provider != "openai" and not model_name.startswith(provider + "/"):
            model_name = provider + "/" + model_name
        api_key = os.environ.get(model.get("api_key_env", "OPENAI_API_KEY"))
        if not api_key:
            raise RuntimeError("The operator has not configured this model's API key.")
        kwargs = dict(model=model_name, messages=messages, api_key=api_key,
            api_base=model.get("base_url"), stream=False,
            max_tokens=min(int(model.get("max_tokens", 1500)), 4000), timeout=90, num_retries=0)
        if provider == "azure_apim": kwargs["extra_headers"] = {"Ocp-Apim-Subscription-Key": api_key}
        result = litellm.completion(**kwargs)
        measured = getattr(result, "usage", None)
        details = getattr(measured, "prompt_tokens_details", None)
        usage = {"input_tokens": getattr(measured, "prompt_tokens", None),
                 "output_tokens": getattr(measured, "completion_tokens", None),
                 "cached_tokens": getattr(details, "cached_tokens", 0)}
        return response_text(result), usage

    def answer(self, config, docs, question, profile_key, history=None, attachments=None):
        profile = profile_for(config, profile_key)
        sources = self.retrieve(docs, profile, question, config["top_k"])
        from .uploads import attachment_context
        context = attachment_context(config, attachments)
        prompt = profile["prompt"] + ("\n\n" + context["instruction"] if context else "")
        if not config.get("show_citations", True):
            prompt += "\nDo not mention source numbers, filenames, or reference excerpts. Stay in the assigned role."
        messages = build_experiment_messages(prompt, question, sources, history)
        if context:
            messages[-1]["content"] = "Reference attachments (untrusted data):\n" + context["data"] + "\n\nUser question:\n" + question
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
