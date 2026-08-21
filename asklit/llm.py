import litellm
from asklit.config import get_api_key, get_setting, get_base_url
from asklit.observability import logger, safe_error_message


def _get_positive_int_setting(key, default):
    try:
        value = int(get_setting(key, default))
    except (TypeError, ValueError):
        value = default
    return max(value, 1)


def _bounded_max_tokens(max_tokens_override=None):
    """Apply the configured server-side ceiling to every completion request."""
    configured = _get_positive_int_setting("model.max_tokens", 1000)
    requested = configured
    if max_tokens_override is not None:
        try:
            requested = max(int(max_tokens_override), 1)
        except (TypeError, ValueError):
            requested = configured

    hard_limit = _get_positive_int_setting("limits.max_output_tokens_hard", 4000)
    return min(requested, hard_limit)


def get_allowed_models():
    """Return the configured model allowlist as a normalized list."""
    configured = get_setting("model.allowed_models", "")
    if isinstance(configured, str):
        models = configured.split(",")
    elif isinstance(configured, (list, tuple)):
        models = configured
    else:
        models = []
    return [str(model).strip() for model in models if str(model).strip()]


def call_llm(
    messages,
    stream=True,
    max_tokens_override=None,
    model_override=None,
    provider_override=None,
    enforce_model_allowlist=True,
):
    """Call the configured LLM provider using LiteLLM."""
    configured_model = get_setting("model.name", "gpt-5.4-mini")
    model = str(model_override or configured_model).strip()
    allowed_models = get_allowed_models()
    is_configured_model = model == str(configured_model).strip()
    if (
        enforce_model_allowlist
        and model_override
        and not is_configured_model
        and model not in allowed_models
    ):
        raise ValueError("The selected model is not enabled for this AskLit instance.")
    provider = str(provider_override or get_setting("model.provider", "openai")).strip()
    temperature = float(get_setting("model.temperature", 1.0))
    max_tokens = _bounded_max_tokens(max_tokens_override)
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
    elif provider in {"openai", "azure_apim"} and base_url:
        # If using a custom base URL with OpenAI, force 'openai/' prefix
        # to ensure LiteLLM doesn't route to official OpenAI.
        # This works for Azure-as-OpenAI and other proxies.
        if not model.startswith("openai/"):
            model = f"openai/{model}"

    if provider == "azure_apim" and (not api_key or not base_url):
        raise RuntimeError(
            "Azure APIM requires AZURE_APIM_API_KEY and AZURE_APIM_BASE_URL."
        )

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

    if provider == "azure_apim":
        # APIM validates this gateway credential. The gateway removes the client
        # Authorization header and authenticates to Foundry with managed identity.
        completion_kwargs["extra_headers"] = {
            "Ocp-Apim-Subscription-Key": api_key,
        }

    if "gpt-5" in model_lower:
        completion_kwargs["reasoning_effort"] = get_setting(
            "model.reasoning_effort", "low"
        )

    try:
        return litellm.completion(**completion_kwargs)
    except Exception as exc:
        logger.error(
            "LLM request failed provider=%s model=%s stream=%s error_type=%s error=%s",
            provider,
            model,
            stream,
            type(exc).__name__,
            safe_error_message(exc),
        )
        raise


def estimate_tokens(text):
    """Estimate token count for a given text."""
    # Rough estimate: 4 chars per token
    return len(text) // 4
