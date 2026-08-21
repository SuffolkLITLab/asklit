import os
import threading
from contextlib import contextmanager

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

import litellm
import streamlit as st
from chromadb import Documents, EmbeddingFunction, Embeddings

from asklit.config import get_api_key, get_base_url, get_setting

CACHE_DIR = os.path.join("data", "model_cache")


def _positive_int_setting(key, default):
    try:
        return max(int(get_setting(key, default)), 1)
    except (TypeError, ValueError):
        return default


_EMBEDDING_SLOTS = None
_EMBEDDING_SLOTS_LOCK = threading.Lock()


def _get_embedding_slots():
    """Create the process-wide embedding queue without config I/O at import."""
    global _EMBEDDING_SLOTS
    if _EMBEDDING_SLOTS is None:
        with _EMBEDDING_SLOTS_LOCK:
            if _EMBEDDING_SLOTS is None:
                _EMBEDDING_SLOTS = threading.BoundedSemaphore(
                    _positive_int_setting("limits.max_concurrent_embedding_jobs", 2)
                )
    return _EMBEDDING_SLOTS


@contextmanager
def embedding_job_slot():
    """Bound memory-heavy embedding work across Streamlit session threads."""
    timeout = _positive_int_setting("limits.embedding_queue_timeout_seconds", 180)
    embedding_slots = _get_embedding_slots()
    acquired = embedding_slots.acquire(timeout=timeout)
    if not acquired:
        raise RuntimeError(
            "The knowledge-base indexer is busy with other users. Please try again."
        )
    try:
        yield
    finally:
        embedding_slots.release()


@st.cache_resource
def get_local_model():
    """Load and cache the local sentence-transformers model."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.environ.setdefault("HF_HOME", CACHE_DIR)
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", CACHE_DIR)
    from sentence_transformers import SentenceTransformer

    model_name = get_setting("model.local_embedding_model", "all-MiniLM-L6-v2")
    return SentenceTransformer(model_name)


def get_remote_embeddings(input_data, model=None):
    """Generate embeddings using a remote provider via LiteLLM."""
    if model is None:
        model = get_setting("model.embedding_model", "text-embedding-3-small")

    provider = get_setting(
        "model.embedding_provider", get_setting("model.provider", "openai")
    )
    if "/" in model:
        provider = model.split("/")[0]

    api_key = get_api_key(provider)
    base_url = get_base_url(provider)

    if provider == "azure" and not model.startswith("azure/"):
        model = f"azure/{model}"
        if base_url and "/openai/v1" in base_url:
            base_url = base_url.split("/openai/v1")[0]
    elif (
        provider in {"openai", "azure_apim"}
        and base_url
        and not model.startswith("openai/")
    ):
        # Force openai/ prefix for custom base URLs to ensure LiteLLM routes correctly.
        model = f"openai/{model}"

    embedding_kwargs = {
        "model": model,
        "input": input_data,
        "api_key": api_key,
        "api_base": base_url,
    }
    if provider == "azure_apim":
        if not api_key or not base_url:
            raise RuntimeError(
                "Azure APIM requires AZURE_APIM_API_KEY and AZURE_APIM_BASE_URL."
            )
        embedding_kwargs["extra_headers"] = {
            "Ocp-Apim-Subscription-Key": api_key,
        }

    with embedding_job_slot():
        response = litellm.embedding(**embedding_kwargs)
    return [item["embedding"] for item in response.data]


def get_embedding(text):
    """Generate embedding for a single text chunk, respecting the local/remote toggle."""
    use_local = str(get_setting("model.use_local_embeddings", "true")).lower() == "true"

    if use_local:
        model = get_local_model()
        with embedding_job_slot():
            return model.encode(text).tolist()
    else:
        return get_remote_embeddings([text])[0]


class LiteLLMEmbeddingFunction(EmbeddingFunction):
    """
    Hybrid embedding function for ChromaDB.
    Can be toggled between local and remote in settings.
    """

    def __call__(self, input: Documents) -> Embeddings:
        if isinstance(input, str):
            input = [input]

        use_local = (
            str(get_setting("model.use_local_embeddings", "true")).lower() == "true"
        )

        if use_local:
            model = get_local_model()
            with embedding_job_slot():
                embeddings = model.encode(input)
            return embeddings.tolist()
        else:
            return get_remote_embeddings(input)
