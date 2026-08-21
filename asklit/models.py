from urllib.parse import urlparse

import requests


MODEL_DISCOVERY_TIMEOUT_SECONDS = 8
AZURE_HOST_SUFFIXES = (
    ".cognitiveservices.azure.com",
    ".openai.azure.com",
    ".services.ai.azure.com",
    ".azure-api.net",
)


def normalize_openai_base_url(value):
    """Validate and normalize a user-configured OpenAI-compatible base URL."""
    value = str(value or "").strip()
    if not value:
        return "", None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "", "Enter a complete http:// or https:// endpoint URL."
    if parsed.username or parsed.password:
        return "", "Do not put credentials in the endpoint URL."
    if parsed.query or parsed.fragment:
        return "", "The endpoint URL cannot contain a query string or fragment."
    return value.rstrip("/"), None


def describe_openai_compatible_endpoint(provider, base_url):
    """Describe the service behind a provider alias without changing its routing."""
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    if host.endswith(".azure-api.net"):
        return "Azure API Management (OpenAI-compatible)"
    if host.endswith(AZURE_HOST_SUFFIXES[:-1]):
        return "Azure AI (OpenAI-compatible)"
    if provider == "openai" and not base_url:
        return "OpenAI"
    if provider == "openai":
        return "Custom OpenAI-compatible endpoint"
    return str(provider or "Unknown provider")


def model_discovery_url(provider, base_url):
    if base_url:
        return f"{str(base_url).rstrip('/')}/models"
    if provider == "openai":
        return "https://api.openai.com/v1/models"
    return None


def discover_available_models(provider, base_url, api_key, timeout=None):
    """Query an OpenAI-compatible /models endpoint and normalize its model IDs."""
    label = describe_openai_compatible_endpoint(provider, base_url)
    url = model_discovery_url(provider, base_url)
    result = {
        "endpoint_label": label,
        "endpoint_host": (urlparse(str(base_url or url or "")).hostname or ""),
        "models": [],
        "error": None,
        "is_azure": label.startswith("Azure"),
    }
    if not url:
        result["error"] = (
            "This provider does not expose an OpenAI-compatible model-list URL."
        )
        return result
    if not api_key:
        result["error"] = "No API credential is configured for model discovery."
        return result

    headers = {"Authorization": f"Bearer {api_key}"}
    if result["is_azure"]:
        headers["api-key"] = api_key
    if provider == "azure_apim":
        headers["Ocp-Apim-Subscription-Key"] = api_key

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=timeout or MODEL_DISCOVERY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        models = []
        seen = set()
        for item in items:
            model_id = item.get("id") if isinstance(item, dict) else None
            model_id = str(model_id or "").strip()
            if model_id and model_id not in seen:
                seen.add(model_id)
                models.append(model_id)
        result["models"] = sorted(models, key=str.casefold)
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        result["error"] = (
            f"Model discovery returned HTTP {status_code}."
            if status_code
            else f"Model discovery failed: {type(exc).__name__}."
        )
    except (TypeError, ValueError):
        result["error"] = "The model endpoint returned an invalid response."
    return result


def choose_model_options(discovery, configured_models, display_limit=20):
    """Prefer verified small lists, but never treat Azure's catalog as deployments."""
    configured = []
    seen = set()
    for item in configured_models or []:
        model = str(item).strip()
        if model and model not in seen:
            seen.add(model)
            configured.append(model)

    discovered = discovery.get("models", [])
    if discovery.get("is_azure"):
        if 0 < len(configured) <= display_limit:
            return configured, "configured Azure deployment allowlist"
        return [], None
    if 0 < len(discovered) <= display_limit:
        return discovered, "endpoint model list"
    if 0 < len(configured) <= display_limit:
        return configured, "configured models"
    return [], None
