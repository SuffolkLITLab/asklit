from itertools import product


MAX_CONTEXT_CHARS = 8000


def build_experiment_messages(system_prompt, user_query, context_chunks):
    """Build a one-turn RAG request from an in-progress scaffold prompt."""
    context_parts = []
    current_length = 0
    for index, chunk in enumerate(context_chunks):
        content = str(chunk.get("content", "")).strip()
        if len(content) < 80:
            continue
        if current_length + len(content) > MAX_CONTEXT_CHARS:
            break
        context_parts.append(f"--- SOURCE {index + 1} ---\n{content}")
        current_length += len(content)

    context = "\n\n".join(context_parts)
    full_system_prompt = (
        f"{system_prompt}\n\n"
        f"RELEVANT CONTEXT FROM THE KNOWLEDGE BASE:\n{context}\n\n"
        "INSTRUCTIONS FOR USING CONTEXT:\n"
        "1. When context is provided and it is relevant, ground the answer in that context before adding general background.\n"
        "2. If the context only partially answers the question, say what the context supports and then add any clearly labeled general guidance.\n"
        "3. If the context does not contain the answer, or if the user is asking a general question, use your general knowledge to provide a helpful response."
    )
    return [
        {"role": "system", "content": full_system_prompt},
        {"role": "user", "content": user_query},
    ]


def parse_model_names(value):
    """Normalize comma- or newline-separated model names without duplicates."""
    if isinstance(value, str):
        candidates = value.replace("\n", ",").split(",")
    else:
        candidates = value or []

    models = []
    seen = set()
    for candidate in candidates:
        model = str(candidate).strip()
        if model and model not in seen:
            models.append(model)
            seen.add(model)
    return models


def build_experiment_matrix(prompt_profiles, prompt_keys, knowledgebase_keys, models):
    """Return the requested prompt × knowledge base × model combinations."""
    profiles_by_key = {profile["key"]: profile for profile in prompt_profiles}
    prompts = [profiles_by_key[key] for key in prompt_keys if key in profiles_by_key]
    knowledgebases = [
        profiles_by_key[key] for key in knowledgebase_keys if key in profiles_by_key
    ]
    return [
        {
            "prompt": prompt_profile,
            "knowledgebase": knowledgebase_profile,
            "model": model,
        }
        for prompt_profile, knowledgebase_profile, model in product(
            prompts, knowledgebases, parse_model_names(models)
        )
    ]


def response_text(response):
    """Extract text from a non-streaming LiteLLM response."""
    choices = getattr(response, "choices", None)
    if not choices and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return ""

    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message", {})
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return str(content or "")
