"""Workspace configuration shapes shared by both scaffolder modes."""

import re

from asklit.config import get_setting

DEFAULT_PROMPT_TEXT = "You are a helpful assistant."
DEFAULT_MODEL_NAME = "gpt-5.4-mini"
PROVIDER_OPTIONS = (
    "openai",
    "azure_apim",
    "anthropic",
    "google",
    "groq",
    "mistral",
)


def slugify_key(value, fallback="default"):
    """Turn a human label into a filesystem- and YAML-safe prompt key."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or fallback


def normalize_prompt_profiles(profiles):
    """Guarantee unique keys and complete fields for every prompt pairing."""
    normalized = []
    used_keys = set()
    for index, profile in enumerate(profiles or []):
        label = str(profile.get("label") or f"Prompt {index + 1}").strip()
        key = slugify_key(profile.get("key") or label, f"prompt-{index + 1}")
        while key in used_keys:
            key = f"{key}-{index + 1}"
        used_keys.add(key)
        normalized.append(
            {
                "key": key,
                "label": label,
                "knowledgebase": slugify_key(
                    profile.get("knowledgebase") or key, "default"
                ),
                "prompt": str(profile.get("prompt") or DEFAULT_PROMPT_TEXT),
                "conversation_starters": [
                    str(starter).strip()
                    for starter in profile.get("conversation_starters") or []
                    if str(starter).strip()
                ],
                "connected_files": [
                    str(filename).strip()
                    for filename in profile.get("connected_files") or []
                    if str(filename).strip()
                ],
            }
        )
    if not normalized:
        normalized.append(
            {
                "key": "default",
                "label": "Default",
                "knowledgebase": "default",
                "prompt": DEFAULT_PROMPT_TEXT,
                "conversation_starters": [],
                "connected_files": [],
            }
        )
    return normalized


def profiles_still_using_the_default_prompt(profiles):
    """Name the prompts nobody has actually written yet.

    A prompt that silently stays at DEFAULT_PROMPT_TEXT reaches evaluation and
    export looking like a real one, so the scores describe the stock assistant
    and the deployed prompts/*.yml ships it. Callers warn on a non-empty list.
    """
    return [
        profile["label"]
        for profile in profiles
        if str(profile.get("prompt") or "").strip() == DEFAULT_PROMPT_TEXT
    ]


def ensure_model_defaults(config_data):
    """Keep Export usable even when someone skips the AI Model step."""
    model = config_data.setdefault("model", {})
    model.setdefault("provider", get_setting("model.provider", "openai"))
    model.setdefault("name", get_setting("model.name", DEFAULT_MODEL_NAME))
    model.setdefault(
        "allow_user_selection",
        str(get_setting("model.allow_user_selection", "false")).lower() == "true",
    )
    model.setdefault("allowed_models", get_setting("model.allowed_models", ""))
    model.setdefault("base_url", "")
    model.setdefault("use_local_embeddings", True)
    model.setdefault("local_embedding_model", "all-MiniLM-L6-v2")
    return config_data


def default_scaffold_config():
    """Return a complete configuration for a new browser workspace.

    New projects start public with the admin backend off; the Export step
    exposes both settings once the learner knows what they are shipping.
    """
    return {
        "app": {
            "title": "My Knowledge Base",
            "welcome_message": "How can I help you today?",
            "access_mode": "public",
            "disable_admin": True,
        },
        "model": {},
        "retrieval": {},
        "limits": {},
        "logging": {"enabled": True},
        "branding": {
            "favicon_url": "https://github.com/SuffolkLITLab/logos/raw/main/current-logo/png/lit-favicon.png",
            "logo_url": "https://github.com/SuffolkLITLab/logos/raw/main/current-logo/png/lit-lab-logo-large.png",
            "homepage_url": "https://suffolklitlab.org",
            "supplemental_footer_text": "",
            "hide_asklit_badge": False,
        },
        "prompt_profiles": [
            {
                "key": "default",
                "label": "Default",
                "knowledgebase": "default",
                "prompt": DEFAULT_PROMPT_TEXT,
                "conversation_starters": [],
                "connected_files": [],
            }
        ],
    }


def merge_workspace_config(defaults, imported):
    """Recursively merge a validated workspace onto current schema defaults."""
    merged = dict(defaults)
    for key, value in imported.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_workspace_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def provider_options(configured_provider, include_azure=False):
    """List selectable providers without dropping one already configured."""
    options = list(PROVIDER_OPTIONS)
    if include_azure:
        options.append("azure")
    if configured_provider and configured_provider not in options:
        options.append(configured_provider)
    return options
