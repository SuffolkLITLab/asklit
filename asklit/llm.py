import litellm
from asklit.config import get_api_key, get_setting, get_base_url


def call_llm(messages, stream=True, max_tokens_override=None):
    """Call the configured LLM provider using LiteLLM."""
    model = get_setting("model.name", "gpt-5-nano")
    provider = get_setting("model.provider", "openai")
    temperature = float(get_setting("model.temperature", 1.0))
    max_tokens = int(max_tokens_override or get_setting("model.max_tokens", 1000))
    disable_temp_setting = get_setting("model.disable_temperature", "false") == "true"

    api_key = get_api_key(provider)
    base_url = get_base_url(provider)

    # Auto-detect models that don't support temperature
    no_temp_families = ["o1-", "o3-", "gpt-5"]
    model_lower = model.lower()
    auto_disable_temp = any(family in model_lower for family in no_temp_families)

    temp_to_pass = temperature
    if disable_temp_setting or auto_disable_temp:
        temp_to_pass = None

    # Routing logic
    # If the user specifically selects 'azure' as provider, use azure/ prefix
    if provider == "azure":
        if not model.startswith("azure/"):
            model = f"azure/{model}"
        # Strip path for Azure SDK logic
        if base_url and "/openai/v1" in base_url:
            base_url = base_url.split("/openai/v1")[0]
    elif provider == "openai" and base_url:
        # If using a custom base URL with OpenAI, force 'openai/' prefix
        # to ensure LiteLLM doesn't route to official OpenAI.
        # This works for Azure-as-OpenAI and other proxies.
        if not model.startswith("openai/"):
            model = f"openai/{model}"

    completion_kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "api_key": api_key,
        "api_base": base_url,
        "stream": stream,
    }

    if temp_to_pass is not None:
        completion_kwargs["temperature"] = temp_to_pass

    if "gpt-5" in model_lower:
        completion_kwargs["reasoning_effort"] = get_setting(
            "model.reasoning_effort", "low"
        )

    response = litellm.completion(**completion_kwargs)
    return response


def estimate_tokens(text):
    """Estimate token count for a given text."""
    # Rough estimate: 4 chars per token
    return len(text) // 4
