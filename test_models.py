from asklit.models import (
    choose_model_options,
    describe_openai_compatible_endpoint,
    discover_available_models,
    normalize_openai_base_url,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_openai_alias_identifies_azure_compatible_endpoint():
    label = describe_openai_compatible_endpoint(
        "openai",
        "https://example.cognitiveservices.azure.com/openai/v1/",
    )

    assert label == "Azure AI (OpenAI-compatible)"


def test_small_compatible_model_list_is_available_for_selection(monkeypatch):
    captured = {}

    def fake_get(url, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return FakeResponse({"data": [{"id": "model-b"}, {"id": "model-a"}]})

    monkeypatch.setattr("asklit.models.requests.get", fake_get)
    discovery = discover_available_models(
        "openai", "https://models.example/v1/", "secret", timeout=3
    )
    choices, source = choose_model_options(discovery, [])

    assert captured["url"] == "https://models.example/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert discovery["models"] == ["model-a", "model-b"]
    assert choices == ["model-a", "model-b"]
    assert source == "endpoint model list"


def test_azure_catalog_is_not_presented_as_deployed_models():
    discovery = {
        "is_azure": True,
        "models": [f"catalog-{index}" for index in range(334)],
    }

    choices, source = choose_model_options(
        discovery,
        ["gpt-5.4-mini", "llama-4-maverick"],
    )

    assert choices == ["gpt-5.4-mini", "llama-4-maverick"]
    assert source == "configured Azure deployment allowlist"


def test_large_non_azure_catalog_uses_manual_entry_without_configuration():
    discovery = {
        "is_azure": False,
        "models": [f"model-{index}" for index in range(21)],
    }

    assert choose_model_options(discovery, []) == ([], None)


def test_custom_openai_base_url_is_normalized_and_rejects_embedded_credentials():
    assert normalize_openai_base_url(" https://models.example/v1/ ") == (
        "https://models.example/v1",
        None,
    )
    normalized, error = normalize_openai_base_url(
        "https://user:password@models.example/v1"
    )
    assert normalized == ""
    assert "credentials" in error


def test_custom_openai_base_url_rejects_query_parameters():
    normalized, error = normalize_openai_base_url(
        "https://models.example/v1?api_key=secret"
    )
    assert normalized == ""
    assert "query string" in error
