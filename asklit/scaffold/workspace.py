"""Save-and-resume workspace files for the scaffolder.

A workspace file must stay safe to email or commit, so credentials, uploaded
document contents, vector indexes, and generated answers never enter it.
"""

import os
from datetime import UTC, datetime

import yaml

from asklit.experiments import normalize_scenario_rows
from asklit.scaffold.config import normalize_prompt_profiles

SENSITIVE_EXPORT_CONFIG_KEYS = {
    "api_key",
    "client_secret",
    "access_token",
    "password",
    "password_hash",
    "shared_password_hash",
    "admin_password_hash",
}
WORKSPACE_SCHEMA_VERSION = 1
MAX_WORKSPACE_YAML_BYTES = 1_000_000


def sanitize_export_config(value):
    """Recursively remove credentials if they ever enter scaffold configuration."""
    if isinstance(value, dict):
        sanitized = {}
        for key, child in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if (
                normalized_key in SENSITIVE_EXPORT_CONFIG_KEYS
                or normalized_key.endswith("_api_key")
            ):
                continue
            sanitized[key] = sanitize_export_config(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_export_config(item) for item in value]
    return value


def export_workspace_yaml(config_data, scenarios, evaluation_rubrics=None):
    """Serialize resumable scaffolder fields without credentials or binary data."""
    safe_config = sanitize_export_config(config_data)
    profiles = normalize_prompt_profiles(safe_config.get("prompt_profiles"))
    source_files = sorted(
        {
            filename
            for profile in profiles
            for filename in profile.get("connected_files", [])
        },
        key=str.casefold,
    )
    uploaded_assets = sorted(
        {
            os.path.basename(str(safe_config.get("branding", {}).get(key, "")))
            for key in ("logo_url", "favicon_url")
            if str(safe_config.get("branding", {}).get(key, "")).startswith("data/")
        },
        key=str.casefold,
    )
    safe_config["prompt_profiles"] = profiles
    payload = {
        "asklit_workspace": {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "app_config": safe_config,
            "evaluation_scenarios": normalize_scenario_rows(scenarios),
            "evaluation_rubrics": [
                str(rubric).strip()
                for rubric in (evaluation_rubrics or [])
                if str(rubric).strip()
            ],
            "source_files_to_reupload": source_files,
            "uploaded_assets_to_reupload": uploaded_assets,
        }
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def import_workspace_yaml(value):
    """Validate and deserialize an AskLit workspace YAML document."""
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, bytes):
        if len(value) > MAX_WORKSPACE_YAML_BYTES:
            raise ValueError("The workspace YAML must be 1 MB or smaller.")
        value = value.decode("utf-8-sig")
    elif len(str(value or "").encode("utf-8")) > MAX_WORKSPACE_YAML_BYTES:
        raise ValueError("The workspace YAML must be 1 MB or smaller.")

    try:
        payload = yaml.safe_load(str(value or ""))
    except yaml.YAMLError as exc:
        raise ValueError("The workspace file is not valid YAML.") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("asklit_workspace"), dict
    ):
        raise ValueError("This is not an AskLit workspace YAML file.")

    workspace = payload["asklit_workspace"]
    if workspace.get("schema_version") != WORKSPACE_SCHEMA_VERSION:
        raise ValueError("This AskLit workspace version is not supported.")
    config_data = workspace.get("app_config")
    if not isinstance(config_data, dict):
        raise ValueError("The workspace is missing its app configuration.")
    scenarios = workspace.get("evaluation_scenarios", [])
    if not isinstance(scenarios, list):
        raise ValueError("The workspace scenarios must be a list.")
    rubrics = workspace.get("evaluation_rubrics", [])
    if not isinstance(rubrics, list) or not all(
        isinstance(rubric, str) for rubric in rubrics
    ):
        raise ValueError("The workspace rubrics must be a list of strings.")

    safe_config = sanitize_export_config(config_data)
    safe_config["prompt_profiles"] = normalize_prompt_profiles(
        safe_config.get("prompt_profiles")
    )
    source_files = {
        str(filename).strip()
        for filename in workspace.get("source_files_to_reupload", [])
        if str(filename).strip()
    }
    for profile in safe_config["prompt_profiles"]:
        source_files.update(profile.get("connected_files", []))
        profile["connected_files"] = []
    uploaded_assets = {
        str(filename).strip()
        for filename in workspace.get("uploaded_assets_to_reupload", [])
        if str(filename).strip()
    }
    branding = safe_config.get("branding", {})
    if isinstance(branding, dict):
        for key in ("logo_url", "favicon_url"):
            value = str(branding.get(key, ""))
            if value.startswith("data/"):
                uploaded_assets.add(os.path.basename(value))
                branding.pop(key, None)

    return {
        "app_config": safe_config,
        "evaluation_scenarios": normalize_scenario_rows(scenarios),
        "evaluation_rubrics": [rubric.strip() for rubric in rubrics if rubric.strip()],
        "source_files_to_reupload": sorted(source_files, key=str.casefold),
        "uploaded_assets_to_reupload": sorted(uploaded_assets, key=str.casefold),
    }
