import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

sys.modules.setdefault("litellm", SimpleNamespace())

from asklit import llm


def test_classroom_burst_is_bounded_by_shared_completion_slots(monkeypatch):
    settings = {
        "model.name": "test-model",
        "model.provider": "openai",
        "model.temperature": "1.0",
        "model.disable_temperature": "false",
        "model.max_tokens": "100",
        "limits.max_output_tokens_hard": "100",
        "limits.llm_queue_timeout_seconds": "5",
    }
    state = {"active": 0, "maximum": 0}
    state_lock = threading.Lock()

    def completion(**_kwargs):
        with state_lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(0.02)
        with state_lock:
            state["active"] -= 1
        return "response"

    monkeypatch.setattr(
        llm, "get_setting", lambda key, default=None: settings.get(key, default)
    )
    monkeypatch.setattr(llm, "get_api_key", lambda _provider: "test-key")
    monkeypatch.setattr(llm, "get_base_url", lambda _provider: None)
    monkeypatch.setattr(llm.litellm, "completion", completion, raising=False)

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(
            executor.map(
                lambda _index: llm.call_llm([{"role": "user", "content": "Hello"}]),
                range(20),
            )
        )

    assert results == ["response"] * 20
    assert 1 < state["maximum"] <= 8


def test_azure_apim_uses_gateway_header_and_openai_compatibility(monkeypatch):
    settings = {
        "model.name": "gpt-5.4-nano",
        "model.provider": "azure_apim",
        "model.temperature": "1.0",
        "model.disable_temperature": "false",
        "model.max_tokens": "1200",
        "model.reasoning_effort": "low",
        "limits.max_output_tokens_hard": "1500",
    }
    captured = {}

    monkeypatch.setattr(
        llm, "get_setting", lambda key, default=None: settings.get(key, default)
    )
    monkeypatch.setattr(llm, "get_api_key", lambda provider: "limited-gateway-key")
    monkeypatch.setattr(
        llm,
        "get_base_url",
        lambda provider: "https://example.azure-api.net/asklit",
    )
    monkeypatch.setattr(
        llm.litellm,
        "completion",
        lambda **kwargs: captured.update(kwargs) or "response",
        raising=False,
    )

    llm.call_llm([{"role": "user", "content": "Hello"}])

    assert captured["model"] == "openai/gpt-5.4-nano"
    assert captured["api_base"] == "https://example.azure-api.net/asklit"
    assert captured["extra_headers"] == {
        "Ocp-Apim-Subscription-Key": "limited-gateway-key"
    }
    assert captured["max_tokens"] == 1200


def test_retry_cannot_exceed_hard_output_ceiling(monkeypatch):
    settings = {
        "model.name": "gpt-5.4-nano",
        "model.provider": "azure_apim",
        "model.temperature": "1.0",
        "model.disable_temperature": "false",
        "model.max_tokens": "1200",
        "model.reasoning_effort": "low",
        "limits.max_output_tokens_hard": "1500",
    }
    captured = {}

    monkeypatch.setattr(
        llm, "get_setting", lambda key, default=None: settings.get(key, default)
    )
    monkeypatch.setattr(llm, "get_api_key", lambda provider: "limited-gateway-key")
    monkeypatch.setattr(
        llm,
        "get_base_url",
        lambda provider: "https://example.azure-api.net/asklit",
    )
    monkeypatch.setattr(
        llm.litellm,
        "completion",
        lambda **kwargs: captured.update(kwargs) or "response",
        raising=False,
    )

    llm.call_llm([{"role": "user", "content": "Hello"}], max_tokens_override=10000)

    assert captured["max_tokens"] == 1500


def test_azure_apim_requires_key_and_url(monkeypatch):
    settings = {
        "model.name": "gpt-5.4-nano",
        "model.provider": "azure_apim",
        "model.temperature": "1.0",
        "model.disable_temperature": "false",
        "model.max_tokens": "1200",
        "limits.max_output_tokens_hard": "1500",
    }
    monkeypatch.setattr(
        llm, "get_setting", lambda key, default=None: settings.get(key, default)
    )
    monkeypatch.setattr(llm, "get_api_key", lambda provider: None)
    monkeypatch.setattr(llm, "get_base_url", lambda provider: None)

    try:
        llm.call_llm([{"role": "user", "content": "Hello"}])
    except RuntimeError as exc:
        assert "AZURE_APIM_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected missing gateway configuration to fail closed")


def test_model_override_must_be_in_configured_allowlist(monkeypatch):
    settings = {
        "model.name": "gpt-5.4-nano",
        "model.allowed_models": "gpt-5.4-nano,llama-4-maverick",
        "model.provider": "azure_apim",
        "model.temperature": "1.0",
        "model.disable_temperature": "false",
        "model.max_tokens": "1200",
        "limits.max_output_tokens_hard": "1500",
    }
    captured = {}
    monkeypatch.setattr(
        llm, "get_setting", lambda key, default=None: settings.get(key, default)
    )
    monkeypatch.setattr(llm, "get_api_key", lambda provider: "limited-gateway-key")
    monkeypatch.setattr(
        llm,
        "get_base_url",
        lambda provider: "https://example.azure-api.net/asklit",
    )
    monkeypatch.setattr(
        llm.litellm,
        "completion",
        lambda **kwargs: captured.update(kwargs) or "response",
        raising=False,
    )

    llm.call_llm(
        [{"role": "user", "content": "Hello"}],
        model_override="llama-4-maverick",
    )
    assert captured["model"] == "openai/llama-4-maverick"

    try:
        llm.call_llm(
            [{"role": "user", "content": "Hello"}],
            model_override="not-approved",
        )
    except ValueError as exc:
        assert "not enabled" in str(exc)
    else:
        raise AssertionError("Expected an unapproved model override to fail closed")


def test_configured_legacy_model_is_valid_when_not_in_optional_allowlist(monkeypatch):
    settings = {
        "model.name": "gpt-5-nano",
        "model.allowed_models": "gpt-5.4-nano,gpt-5.4-mini",
        "model.provider": "openai",
        "model.temperature": "1.0",
        "model.disable_temperature": "false",
        "model.max_tokens": "100",
        "limits.max_output_tokens_hard": "4000",
    }
    captured = {}
    monkeypatch.setattr(
        llm, "get_setting", lambda key, default=None: settings.get(key, default)
    )
    monkeypatch.setattr(llm, "get_api_key", lambda provider: "existing-app-key")
    monkeypatch.setattr(
        llm,
        "get_base_url",
        lambda provider: "https://example.openai.azure.com/openai/v1",
    )
    monkeypatch.setattr(
        llm.litellm,
        "completion",
        lambda **kwargs: captured.update(kwargs) or "response",
        raising=False,
    )

    llm.call_llm(
        [{"role": "user", "content": "Hello"}],
        model_override="gpt-5-nano",
    )

    assert captured["model"] == "openai/gpt-5-nano"
