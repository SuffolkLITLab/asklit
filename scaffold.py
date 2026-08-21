import os
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import UTC, datetime

import pandas as pd
import streamlit as st
import toml
import yaml

from asklit.auth import hash_password
from asklit.config import get_api_key, get_base_url, get_secret_value, get_setting
from asklit.db import get_connection, init_db
from asklit.experiments import (
    build_evaluation_matrix,
    build_experiment_messages,
    build_rubric_judge_messages,
    evaluate_expected,
    is_model_rubric,
    normalize_scenario_rows,
    parse_generated_scenarios,
    parse_model_names,
    parse_rubric_grade,
    rubric_text,
    parse_scenario_csv,
    response_text,
    scenarios_to_csv,
)
from asklit.github import (
    GitHubError,
    get_authenticated_user,
    poll_device_token,
    publish_directory,
    request_device_code,
)
from asklit.ingestion import chunk_pages, extract_text, get_content_hash
from asklit.llm import call_llm, estimate_tokens
from asklit.models import (
    choose_model_options,
    discover_available_models,
    normalize_openai_base_url,
)
from asklit.observability import log_ai_call_event, safe_error_message
from asklit.rag import add_document_to_index, query_index
from asklit.ui import escape_html, safe_url

# Constants
DEFAULT_REPO_NAME = "my-asklit-app"
REQUEST_TIMEOUT_SECONDS = 20
RUNTIME_ROOT_FILES = {
    "runtime/app.py": "app.py",
    "chat_ui.py": "chat_ui.py",
    "login_ui.py": "login_ui.py",
    "requirements.txt": "requirements.txt",
}
RUNTIME_DIRECTORIES = ("admin",)
RUNTIME_ASKLIT_MODULES = (
    "__init__.py",
    "auth.py",
    "config.py",
    "db.py",
    "embeddings.py",
    "ingestion.py",
    "llm.py",
    "observability.py",
    "prompts.py",
    "rag.py",
    "rate_limits.py",
    "ui.py",
)
RECURSIVE_ARTIFACT_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".DS_Store",
    ".coverage",
    "htmlcov",
}
RECURSIVE_ARTIFACT_SUFFIXES = (".pyc", ".pyo", "-wal", "-shm")
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


def ignore_bundle_artifacts(_directory, names):
    """Filter generated/runtime artifacts at every depth of a scaffold copy."""
    return [
        name
        for name in names
        if name in RECURSIVE_ARTIFACT_NAMES
        or name.endswith(RECURSIVE_ARTIFACT_SUFFIXES)
    ]


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


def export_workspace_yaml(config_data, scenarios):
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
        "source_files_to_reupload": sorted(source_files, key=str.casefold),
        "uploaded_assets_to_reupload": sorted(uploaded_assets, key=str.casefold),
    }


def render_password_hash_setup(label, state_key, help_text):
    """Collect a password once, retain only its PBKDF2 hash, and show the result."""
    configured_hash = st.session_state.get(state_key)
    if configured_hash:
        st.success(f"{label} is configured.")
        st.caption(
            "Generated password hash (this will be placed in deployment secrets):"
        )
        st.code(configured_hash, language=None)
        if st.button(f"Change {label.lower()}", key=f"change_{state_key}"):
            st.session_state.pop(state_key, None)
            st.rerun()
        return configured_hash

    st.caption(help_text)
    with st.form(f"form_{state_key}", clear_on_submit=True):
        password = st.text_input(label, type="password", key=f"input_{state_key}")
        confirmation = st.text_input(
            f"Confirm {label.lower()}",
            type="password",
            key=f"confirm_{state_key}",
        )
        submitted = st.form_submit_button(f"Set {label.lower()}")
    if submitted:
        if len(password) < 8:
            st.error("Use at least 8 characters.")
        elif password != confirmation:
            st.error("The passwords do not match.")
        else:
            st.session_state[state_key] = hash_password(password)
            st.rerun()
    return None


@st.cache_data(ttl=300, show_spinner=False)
def discover_endpoint_models(provider, base_url):
    """Cache public model metadata without placing credentials in the cache key."""
    return discover_available_models(
        provider,
        base_url,
        get_api_key(provider),
    )


def get_endpoint_model_choices(provider, configured_models, base_url_override=None):
    """Choose honest runnable options without mistaking Azure's catalog for deployments."""
    base_url = (
        base_url_override if base_url_override is not None else get_base_url(provider)
    )
    discovery = discover_endpoint_models(provider, base_url)
    configured_models = parse_model_names(configured_models)

    choices, choice_source = choose_model_options(discovery, configured_models)
    return choices, choice_source, discovery


def render_endpoint_model_status(discovery, choice_source):
    host = discovery.get("endpoint_host") or "default endpoint"
    st.caption(f"Endpoint: {discovery['endpoint_label']} · {host}")
    count = len(discovery["models"])
    if discovery["is_azure"] and count:
        st.caption(
            f"Azure `/models` returned {count} catalog entries; these are not treated "
            f"as deployed models. Showing {choice_source or 'manual model entry'}."
        )
    elif choice_source:
        st.caption(f"Showing {choice_source}.")
    elif discovery["error"]:
        st.caption(f"Automatic model discovery unavailable: {discovery['error']}")
    elif count > 20:
        st.caption(
            f"The endpoint returned {count} models, so manual entry remains available."
        )


def create_bundle(config_data, data_dir_source):
    """Create a deployable chatbot runtime with generated config and data."""
    temp_dir = tempfile.mkdtemp()
    root_dir = os.path.dirname(os.path.abspath(__file__))

    # 1. Copy only the production chatbot runtime. Scaffolder, experiment,
    # OAuth-publishing, test, documentation, and infrastructure files stay out.
    for source_name, destination_name in RUNTIME_ROOT_FILES.items():
        shutil.copy2(
            os.path.join(root_dir, source_name),
            os.path.join(temp_dir, destination_name),
        )

    for directory in RUNTIME_DIRECTORIES:
        shutil.copytree(
            os.path.join(root_dir, directory),
            os.path.join(temp_dir, directory),
            ignore=ignore_bundle_artifacts,
        )

    asklit_dir = os.path.join(temp_dir, "asklit")
    os.makedirs(asklit_dir, exist_ok=True)
    for filename in RUNTIME_ASKLIT_MODULES:
        shutil.copy2(
            os.path.join(root_dir, "asklit", filename),
            os.path.join(asklit_dir, filename),
        )

    # 2. Write the new defaults.toml
    config_path = os.path.join(temp_dir, "config", "defaults.toml")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    # Prompt pairings are written as YAML files under prompts/.
    toml_data = sanitize_export_config(config_data)
    prompt_profiles = normalize_prompt_profiles(toml_data.pop("prompt_profiles", []))
    toml_data.pop("prompt", None)
    with open(config_path, "w") as f:
        toml.dump(toml_data, f)

    # 3. Copy the built data directory (SQLite + Chroma). Classroom sessions use
    # WAL for concurrent diagnostics; checkpoint first and never export sidecars.
    source_db_path = os.path.join(data_dir_source, "app.sqlite3")
    if os.path.exists(source_db_path):
        conn = get_connection(db_path=source_db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    dst_data_dir = os.path.join(temp_dir, "data")
    if os.path.exists(dst_data_dir):
        shutil.rmtree(dst_data_dir)
    shutil.copytree(
        data_dir_source,
        dst_data_dir,
        ignore=ignore_bundle_artifacts,
    )

    # 4. Save prompt/knowledgebase pairings as deployable YAML configs.
    prompts_dir = os.path.join(temp_dir, "prompts")
    if os.path.exists(prompts_dir):
        shutil.rmtree(prompts_dir)
    os.makedirs(prompts_dir, exist_ok=True)
    for profile in prompt_profiles:
        prompt_path = os.path.join(prompts_dir, f"{profile['key']}.yml")
        with open(prompt_path, "w") as f:
            yaml.safe_dump(
                {
                    "label": profile["label"],
                    "knowledgebase": {
                        "name": profile["knowledgebase"],
                        "files": profile["connected_files"],
                    },
                    "prompt": profile["prompt"],
                    "conversation starters": profile["conversation_starters"],
                },
                f,
                sort_keys=False,
            )

    # 4. Create a .streamlit/config.toml (standard)
    st_config_dir = os.path.join(temp_dir, ".streamlit")
    os.makedirs(st_config_dir, exist_ok=True)
    with open(os.path.join(st_config_dir, "config.toml"), "w") as f:
        f.write(
            "[server]\nheadless = true\nenableCORS = false\n"
            "enableXsrfProtection = false\nmaxUploadSize = 10\n"
        )

    # 5. Add runtime-focused repository metadata and setup instructions.
    with open(os.path.join(temp_dir, ".gitignore"), "w") as f:
        f.write(
            "__pycache__/\n*.py[cod]\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
            ".venv/\nvenv/\n.env\n.DS_Store\n.streamlit/secrets.toml\n"
            "data/model_cache/\n"
        )

    with open(os.path.join(temp_dir, "README.md"), "w") as f:
        f.write(f"# {config_data['app']['title']}\n\n")
        f.write("This app was generated by the AskLit Scaffolder.\n\n")
        f.write("## Setup\n1. Deploy this repository to Streamlit Cloud.\n")
        f.write("2. Set your API keys in the Streamlit Secrets manager.\n")
        f.write("3. You're ready to go!\n")

    return temp_dir


def slugify_key(value, fallback="default"):
    value = "".join(char.lower() if char.isalnum() else "-" for char in value or "")
    value = "-".join(part for part in value.split("-") if part)
    return value or fallback


def normalize_prompt_profiles(profiles):
    if not profiles:
        profiles = [
            {
                "key": "default",
                "label": "Default",
                "knowledgebase": "default",
                "prompt": "You are a helpful assistant.",
                "conversation_starters": [],
                "connected_files": [],
            }
        ]

    normalized = []
    seen = set()
    for index, profile in enumerate(profiles):
        label = str(profile.get("label") or f"Prompt {index + 1}").strip()
        key = slugify_key(profile.get("key") or label, "default")
        if index == 0 and key == "default-system-prompt":
            key = "default"
        base_key = key
        suffix = 2
        while key in seen:
            key = f"{base_key}-{suffix}"
            suffix += 1
        seen.add(key)
        knowledgebase = str(profile.get("knowledgebase") or key).strip() or "default"
        normalized.append(
            {
                "key": key,
                "label": label,
                "knowledgebase": knowledgebase,
                "prompt": str(profile.get("prompt") or "You are a helpful assistant."),
                "conversation_starters": [
                    str(starter).strip()
                    for starter in profile.get("conversation_starters", [])
                    if str(starter).strip()
                ],
                "connected_files": [
                    str(filename).strip()
                    for filename in profile.get("connected_files", [])
                    if str(filename).strip()
                ],
            }
        )

    return normalized


def zip_directory(path, output_path):
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as ziph:
        for root, dirs, files in os.walk(path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, path)
                ziph.write(file_path, arcname)


def toml_quote(value):
    return toml.dumps({"value": str(value)}).split("=", 1)[1].strip()


def generate_deployment_secrets(config_data, password_hashes=None):
    """Return copy-ready Streamlit secrets with placeholder API credentials."""
    password_hashes = password_hashes or {}
    provider = config_data["model"].get("provider", "openai")
    lines = [
        "# AskLit deployment secrets",
        "# Replace every PASTE_... value in Streamlit. Never add this file to GitHub.",
        f'{provider.upper()}_API_KEY = "PASTE_YOUR_KEY_HERE"',
    ]
    if provider == "openai" and config_data["model"].get("base_url"):
        lines.append(
            f"OPENAI_BASE_URL = {toml_quote(config_data['model']['base_url'])}"
        )
    if provider == "azure_apim":
        gateway_url = config_data["model"].get("base_url") or (
            "https://YOUR-GATEWAY.azure-api.net/asklit"
        )
        lines.extend(
            [
                f"AZURE_APIM_BASE_URL = {toml_quote(gateway_url)}",
                "",
                '"model.provider" = "azure_apim"',
                f'"model.name" = {toml_quote(config_data["model"].get("name", ""))}',
                f'"model.allow_user_selection" = {str(config_data["model"].get("allow_user_selection", True)).lower()}',
                f'"model.allowed_models" = {toml_quote(config_data["model"].get("allowed_models", ""))}',
                '"model.use_local_embeddings" = true',
                '"model.local_embedding_model" = "all-MiniLM-L6-v2"',
                '"limits.max_output_tokens_hard" = "4000"',
                '"limits.max_conversation_turns" = "30"',
            ]
        )
    lines.extend(["", "# Identity & access", 'ADMIN_ROUTE = "manage"'])
    if not config_data["app"].get("disable_admin"):
        lines.append(
            f"ADMIN_PASSWORD_HASH = {toml_quote(password_hashes.get('admin') or 'PASTE_ADMIN_HASH_HERE')}"
        )
    if config_data["app"]["access_mode"] == "password":
        lines.append(
            f"SHARED_PASSWORD_HASH = {toml_quote(password_hashes.get('shared') or 'PASTE_SHARED_HASH_HERE')}"
        )
    lines.extend(
        [
            "",
            "# App overrides",
            f'"app.title" = {toml_quote(config_data["app"]["title"])}',
            f'"app.access_mode" = {toml_quote(config_data["app"]["access_mode"])}',
            f'"app.disable_admin" = {str(config_data["app"].get("disable_admin", False)).lower()}',
        ]
    )
    return "\n".join(lines) + "\n"


def ensure_model_defaults(config_data):
    """Keep Export usable even when someone skips the AI Model step."""
    model = config_data.setdefault("model", {})
    model.setdefault("provider", get_setting("model.provider", "openai"))
    model.setdefault("name", get_setting("model.name", "gpt-5.4-mini"))
    model.setdefault(
        "allow_user_selection",
        str(get_setting("model.allow_user_selection", "false")).lower() == "true",
    )
    model.setdefault("allowed_models", get_setting("model.allowed_models", ""))
    model.setdefault("base_url", "")
    model.setdefault("use_local_embeddings", True)
    model.setdefault("local_embedding_model", "all-MiniLM-L6-v2")
    return config_data


def get_scaffold_document_labels(db_path):
    """Return document labels for experiment citations."""
    conn = get_connection(db_path=db_path)
    rows = conn.execute("SELECT id, filename FROM documents").fetchall()
    conn.close()
    return {row["id"]: row["filename"] for row in rows}


def get_knowledgebase_sample(db_path, knowledgebase, max_chars=12000):
    """Return a bounded source sample for generating grounded scenarios."""
    conn = get_connection(db_path=db_path)
    rows = conn.execute(
        """
        SELECT dc.content
        FROM document_chunks AS dc
        JOIN documents AS d ON d.id = dc.document_id
        WHERE d.knowledgebase = ?
        ORDER BY d.filename, dc.chunk_index
        LIMIT 30
        """,
        (knowledgebase,),
    ).fetchall()
    conn.close()
    sample = "\n\n".join(str(row["content"]) for row in rows)
    return sample[:max_chars]


def render_experiment_lab(playground=False):
    """Render editable scenario evaluation in single-model or matrix mode."""
    ensure_model_defaults(st.session_state.app_config)
    profiles = normalize_prompt_profiles(
        st.session_state.app_config.get("prompt_profiles")
    )
    st.session_state.app_config["prompt_profiles"] = profiles
    model_config = st.session_state.app_config["model"]

    st.header("Evaluate your playground" if playground else "Step 4: Experiment Lab")
    st.write(
        "Build a gold-labeled scenario set, then test one model or run every "
        "selected prompt and model as a matrix. Results stay in this browser "
        "session and are not included in an exported app."
    )
    st.warning(
        "Each combination makes a real model call using credentials configured for this scaffolder. "
        "Your provider may charge for these calls."
    )

    provider_options = [
        "openai",
        "azure_apim",
        "anthropic",
        "google",
        "groq",
        "mistral",
        "azure",
    ]
    configured_provider = model_config.get("provider", "openai")
    if configured_provider not in provider_options:
        provider_options.append(configured_provider)
    provider = st.selectbox(
        "Provider",
        provider_options,
        index=provider_options.index(configured_provider),
    )
    configured_endpoint_url = (
        str(model_config.get("base_url", "")).strip()
        if provider == configured_provider and provider in {"openai", "azure_apim"}
        else ""
    )
    trusted_endpoint_url = str(get_base_url(provider) or "").rstrip("/")
    untrusted_custom_endpoint = bool(
        configured_endpoint_url
        and (
            provider == "openai"
            or not trusted_endpoint_url
            or configured_endpoint_url.rstrip("/") != trusted_endpoint_url
        )
    )
    provider_ready = bool(get_api_key(provider))
    if provider == "azure_apim":
        provider_ready = provider_ready and bool(get_base_url(provider))
    if untrusted_custom_endpoint:
        provider_ready = False
        st.warning(
            "Experiments against a user-supplied endpoint are disabled in the "
            "public scaffolder so its central API key is never sent to that host. "
            "The generated app will use the endpoint with your own key."
        )
    elif not provider_ready:
        st.error(
            f"The scaffolder host has no usable {provider} credentials. "
            "Choose the provider configured by the scaffolder administrator."
        )
    configured_models = parse_model_names(
        [
            model_config.get("name", ""),
            *parse_model_names(model_config.get("allowed_models", "")),
        ]
    )
    configured_for_provider = (
        configured_models if provider == model_config.get("provider") else []
    )
    if untrusted_custom_endpoint:
        model_choices, choice_source, discovery = [], None, None
    else:
        model_choices, choice_source, discovery = get_endpoint_model_choices(
            provider, configured_for_provider
        )
        render_endpoint_model_status(discovery, choice_source)
    configured_model = str(model_config.get("name", "")).strip()
    default_model = (
        configured_model
        if configured_model in model_choices or not model_choices
        else model_choices[0]
    )

    st.subheader("1. Gold-labeled scenarios")
    st.caption(
        "Edit cells directly or upload a UTF-8 CSV. AskLit accepts input/question/query "
        "and gold_label/expected/reference_answer aliases, plus Promptfoo-style "
        "__expected, __description, and __metadata:* columns."
    )
    with st.expander("How to write a model-graded rubric", expanded=False):
        st.markdown(
            "Use `llm-rubric:` followed by the qualities a good answer must have. "
            "The selected judge model reads the question, the generated answer, "
            "and this rubric, then assigns a score from 0 to 1. A score of 0.70 "
            "or higher passes."
        )
        st.code(
            "llm-rubric:Explains the next practical step, stays grounded in the guide, "
            "and says when the guide does not provide enough information",
            language="text",
        )
        st.markdown(
            "Use exact or `icontains:` labels when a specific phrase must appear. "
            "Use a rubric when equivalent wording should receive credit. Rubric "
            "grading makes an additional model call for every scenario × prompt × "
            "model combination, so start with a small set and review the judge "
            "rationale before trusting the pass rate."
        )
    if "evaluation_scenarios" not in st.session_state:
        st.session_state.evaluation_scenarios = [
            {
                "input": "What is the most important fact a user should know?",
                "__expected": "",
                "__description": "Core knowledge",
            }
        ]

    uploaded_scenarios = st.file_uploader(
        "Upload scenario CSV", type=["csv"], key="evaluation_scenario_upload"
    )
    if uploaded_scenarios is not None:
        signature = (uploaded_scenarios.name, uploaded_scenarios.size)
        if st.session_state.get("evaluation_upload_signature") != signature:
            try:
                st.session_state.evaluation_scenarios = parse_scenario_csv(
                    uploaded_scenarios.getvalue()
                )
                st.session_state.evaluation_upload_signature = signature
                st.session_state.evaluation_editor_version = (
                    st.session_state.get("evaluation_editor_version", 0) + 1
                )
                st.success(
                    f"Loaded {len(st.session_state.evaluation_scenarios)} scenarios."
                )
            except (UnicodeDecodeError, ValueError) as exc:
                st.error(str(exc))

    scenario_frame = pd.DataFrame(st.session_state.evaluation_scenarios)
    for required_column in ("input", "__expected", "__description"):
        if required_column not in scenario_frame:
            scenario_frame[required_column] = ""
    edited_frame = st.data_editor(
        scenario_frame,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_order=["input", "__expected", "__description"],
        column_config={
            "input": st.column_config.TextColumn("Input", width="large", required=True),
            "__expected": st.column_config.TextColumn(
                "Gold label / __expected",
                width="large",
                help=(
                    "Plain text means exact match. Also supports contains:, icontains:, "
                    "contains-any:, contains-all:, and llm-rubric:criteria. "
                    "Rubric labels use the selected judge model."
                ),
            ),
            "__description": st.column_config.TextColumn("Description", width="medium"),
        },
        key=(
            f"evaluation_scenario_editor_"
            f"{st.session_state.get('evaluation_editor_version', 0)}"
        ),
    )
    st.session_state.evaluation_scenarios = normalize_scenario_rows(
        edited_frame.fillna("").to_dict("records")
    )
    st.download_button(
        "Download scenarios as CSV",
        scenarios_to_csv(st.session_state.evaluation_scenarios),
        "asklit-scenarios.csv",
        "text/csv",
    )

    labels = {profile["key"]: profile["label"] for profile in profiles}
    profile_keys = [profile["key"] for profile in profiles]
    generation_prompt_key = st.selectbox(
        "Prompt for scenario generation",
        profile_keys,
        format_func=lambda key: labels[key],
    )
    generation_profile = next(
        profile for profile in profiles if profile["key"] == generation_prompt_key
    )
    generation_count = st.slider("Scenarios to generate", 1, 12, 5)
    if st.button(
        "Generate gold-labeled scenarios",
        disabled=not provider_ready or not default_model,
    ):
        db_path = os.path.join(st.session_state.temp_data_dir, "app.sqlite3")
        source_sample = get_knowledgebase_sample(
            db_path, generation_profile["knowledgebase"]
        )
        generation_messages = [
            {
                "role": "system",
                "content": (
                    "You create concise evaluation datasets. Return only a JSON array. "
                    "Each object must contain input, __expected, and __description. "
                    "Write realistic user questions answerable from the supplied material. "
                    "Use an icontains: gold label containing the shortest decisive phrase "
                    "that a correct answer must include. Do not use facts absent from the material."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Create {generation_count} diverse scenarios.\n\n"
                    f"SYSTEM PROMPT:\n{generation_profile['prompt']}\n\n"
                    f"KNOWLEDGE BASE SAMPLE:\n{source_sample or '[No documents uploaded]'}"
                ),
            },
        ]
        try:
            with st.spinner("Generating scenarios…"):
                generated_response = call_llm(
                    generation_messages,
                    stream=False,
                    max_tokens_override=2000,
                    model_override=default_model,
                    provider_override=provider,
                    enforce_model_allowlist=False,
                )
                generated = parse_generated_scenarios(response_text(generated_response))
            if not generated:
                st.error("The model returned no usable scenarios.")
            else:
                st.session_state.evaluation_scenarios = generated
                st.session_state.evaluation_editor_version = (
                    st.session_state.get("evaluation_editor_version", 0) + 1
                )
                st.rerun()
        except Exception as exc:
            st.error(f"Scenario generation failed: {exc}")

    st.subheader("2. Run settings")
    evaluation_mode = st.radio(
        "Evaluation shape",
        ["Single model", "Prompt × model matrix"],
        horizontal=True,
        help=(
            "Single model runs every scenario once. Matrix mode runs every scenario "
            "through every selected prompt, knowledge base, and model."
        ),
    )
    if evaluation_mode == "Single model":
        prompt_keys = [
            st.selectbox(
                "Prompt",
                profile_keys,
                format_func=lambda key: labels[key],
                key="single_evaluation_prompt",
            )
        ]
        knowledgebase_keys = [
            st.selectbox(
                "Knowledge base",
                profile_keys,
                format_func=lambda key: (
                    f"{labels[key]} ({next(profile['knowledgebase'] for profile in profiles if profile['key'] == key)})"
                ),
                key="single_evaluation_knowledgebase",
            )
        ]
        if model_choices:
            model_names = [
                st.selectbox(
                    "Model",
                    model_choices,
                    index=model_choices.index(default_model),
                    key="single_evaluation_model",
                )
            ]
        else:
            model_names = st.text_input(
                "Model", value=default_model, key="single_evaluation_model_text"
            )
    else:
        prompt_keys = st.multiselect(
            "Prompts",
            profile_keys,
            default=[profile_keys[0]],
            format_func=lambda key: labels[key],
        )
        knowledgebase_keys = st.multiselect(
            "Knowledge bases",
            profile_keys,
            default=[profile_keys[0]],
            format_func=lambda key: (
                f"{labels[key]} ({next(profile['knowledgebase'] for profile in profiles if profile['key'] == key)})"
            ),
        )
        if model_choices:
            model_names = st.multiselect(
                "Models", model_choices, default=[default_model]
            )
        else:
            model_names = st.text_area(
                "Models (one per line or comma-separated)", value=default_model
            )
    rubric_scenarios = [
        scenario
        for scenario in st.session_state.evaluation_scenarios
        if is_model_rubric(scenario.get("__expected", ""))
    ]
    judge_model = ""
    if rubric_scenarios:
        st.info(
            "This scenario set includes model-graded rubrics. Use "
            "llm-rubric:your criteria in the Gold label column. The judge "
            "model grades the answer on a 0–1 scale; 0.70 or higher passes."
        )
        if model_choices:
            judge_model = st.selectbox(
                "Judge model",
                model_choices,
                index=model_choices.index(default_model),
                help=(
                    "The judge is called separately for each rubric scenario. "
                    "Choose a capable model, and remember that judge calls add cost."
                ),
            )
        else:
            judge_model = st.text_input(
                "Judge model",
                value=default_model,
                help=(
                    "The judge is called separately for each rubric scenario. "
                    "Judge calls add cost."
                ),
            )

    top_k = st.slider("Retrieved passages per run", 1, 10, 5)

    matrix = build_evaluation_matrix(
        profiles,
        prompt_keys,
        knowledgebase_keys,
        model_names,
        st.session_state.evaluation_scenarios,
    )
    run_count = len(matrix)
    judge_run_count = run_count if rubric_scenarios else 0
    if run_count:
        call_summary = f"{run_count} answer model call{'s' if run_count != 1 else ''}"
        if judge_run_count:
            call_summary += (
                f" + {judge_run_count} judge call{'s' if judge_run_count != 1 else ''}"
            )
        st.caption(call_summary + " will run.")
    if run_count > 60:
        st.error("Reduce the matrix to 60 model calls or fewer.")

    can_run = bool(
        matrix
        and run_count <= 60
        and provider_ready
        and (not rubric_scenarios or judge_model)
    )
    if st.button("Run evaluation", type="primary", disabled=not can_run):
        experiment_run_id = str(uuid.uuid4())
        db_path = os.path.join(st.session_state.temp_data_dir, "app.sqlite3")
        chroma_path = os.path.join(st.session_state.temp_data_dir, "chroma")
        document_labels = get_scaffold_document_labels(db_path)
        results = []

        progress = st.progress(0, text="Starting experiment…")
        for index, combination in enumerate(matrix):
            prompt_profile = combination["prompt"]
            knowledgebase_profile = combination["knowledgebase"]
            model = combination["model"]
            scenario = combination["scenario"]
            question = scenario["input"]
            progress.progress(
                index / run_count,
                text=(
                    f"Scenario {index + 1}/{run_count}: "
                    f"{prompt_profile['label']} × {model}"
                ),
            )
            started = time.perf_counter()
            context_chunks = []
            answer = ""
            error = None
            failure_stage = None
            try:
                context_chunks = query_index(
                    question,
                    n_results=top_k,
                    knowledgebase=knowledgebase_profile["knowledgebase"],
                    connected_files=knowledgebase_profile.get("connected_files"),
                    db_path=db_path,
                    chroma_path=chroma_path,
                )
            except Exception as exc:
                error = exc
                failure_stage = "retrieval"

            if error is None:
                messages = build_experiment_messages(
                    prompt_profile["prompt"], question, context_chunks
                )
                try:
                    response = call_llm(
                        messages,
                        stream=False,
                        model_override=model,
                        provider_override=provider,
                        enforce_model_allowlist=False,
                    )
                    answer = response_text(response)
                    if not answer:
                        error = RuntimeError("The model returned an empty response.")
                        failure_stage = "response"
                except Exception as exc:
                    error = exc
                    failure_stage = "completion"

            elapsed = time.perf_counter() - started
            input_tokens = (
                estimate_tokens(question)
                + estimate_tokens(prompt_profile["prompt"])
                + sum(
                    estimate_tokens(chunk.get("content", ""))
                    for chunk in context_chunks
                )
            )
            safe_error = log_ai_call_event(
                run_id=experiment_run_id,
                source="experiment_lab",
                provider=provider,
                model=model,
                prompt_key=prompt_profile["key"],
                knowledgebase=knowledgebase_profile["knowledgebase"],
                status="failed" if error else "succeeded",
                stage=failure_stage or "completion",
                error=error,
                latency_ms=round(elapsed * 1000),
                tokens_in=input_tokens,
                tokens_out=estimate_tokens(answer),
            )
            grade = (
                {
                    "passed": None,
                    "score": None,
                    "reason": "Model call failed",
                }
                if error
                else evaluate_expected(answer, scenario.get("__expected", ""))
            )
            expected = scenario.get("__expected", "")
            if error is None and is_model_rubric(expected):
                try:
                    judge_response = call_llm(
                        build_rubric_judge_messages(
                            question, answer, rubric_text(expected)
                        ),
                        stream=False,
                        max_tokens_override=300,
                        model_override=judge_model,
                        provider_override=provider,
                        enforce_model_allowlist=False,
                    )
                    grade = parse_rubric_grade(response_text(judge_response))
                except Exception as exc:
                    grade = {
                        "passed": None,
                        "score": None,
                        "reason": "Model rubric judge failed",
                    }
                    error = exc
                    safe_error = safe_error_message(exc)
                    failure_stage = "judge"

            results.append(
                {
                    "run_id": experiment_run_id,
                    "prompt_label": prompt_profile["label"],
                    "knowledgebase_label": knowledgebase_profile["label"],
                    "knowledgebase": knowledgebase_profile["knowledgebase"],
                    "model": model,
                    "provider": provider,
                    "scenario": scenario.get("__description") or question,
                    "input": question,
                    "expected": scenario.get("__expected", ""),
                    "answer": answer,
                    "grader": "model rubric" if is_model_rubric(expected) else "deterministic",
                    "judge_model": judge_model if is_model_rubric(expected) else "",
                    "passed": grade["passed"],
                    "score": grade["score"],
                    "grade_reason": grade["reason"],
                    "error": safe_error,
                    "failure_stage": failure_stage,
                    "elapsed": elapsed,
                    "tokens": input_tokens + estimate_tokens(answer),
                    "sources": [
                        {
                            "filename": document_labels.get(
                                chunk.get("metadata", {}).get("document_id"),
                                "Knowledge base document",
                            ),
                            "page": chunk.get("metadata", {}).get("page_number"),
                            "content": chunk.get("content", ""),
                        }
                        for chunk in context_chunks
                    ],
                }
            )
        progress.progress(1.0, text="Experiment complete")
        st.session_state.experiment_results = results

    results = st.session_state.get("experiment_results", [])
    if results:
        st.subheader("3. Results")
        passed = sum(result["passed"] is True for result in results)
        graded = sum(result["passed"] is not None for result in results)
        metric_columns = st.columns(3)
        metric_columns[0].metric("Runs", len(results))
        metric_columns[1].metric("Graded", graded)
        metric_columns[2].metric(
            "Pass rate", f"{passed / graded:.0%}" if graded else "—"
        )

        filter_columns = st.columns(3)
        prompt_filter = filter_columns[0].multiselect(
            "Filter prompts",
            sorted({result["prompt_label"] for result in results}),
        )
        model_filter = filter_columns[1].multiselect(
            "Filter models", sorted({result["model"] for result in results})
        )
        outcome_filter = filter_columns[2].multiselect(
            "Filter outcomes", ["Pass", "Fail", "Not graded", "Error"]
        )

        all_table_rows = []
        for result in results:
            outcome = (
                "Error"
                if result["error"]
                else "Pass"
                if result["passed"] is True
                else "Fail"
                if result["passed"] is False
                else "Not graded"
            )
            source_labels = ", ".join(
                f"{source['filename']} p.{source['page']}"
                for source in result["sources"]
            )
            retrieved_context = "\n\n".join(
                f"[{source['filename']} p.{source['page']}]\n{source['content']}"
                for source in result["sources"]
            )
            all_table_rows.append(
                {
                    "Run ID": result["run_id"],
                    "Outcome": outcome,
                    "Scenario": result["scenario"],
                    "Input": result["input"],
                    "Prompt": result["prompt_label"],
                    "Knowledge base": result["knowledgebase_label"],
                    "Model": result["model"],
                    "Provider": result["provider"],
                    "Grader": result.get("grader", "deterministic"),
                    "Judge model": result.get("judge_model", ""),
                    "Gold label": result["expected"],
                    "Answer": result["answer"],
                    "Grade": result["grade_reason"],
                    "Score": result["score"],
                    "Latency (s)": round(result["elapsed"], 2),
                    "Approx. tokens": result["tokens"],
                    "Sources": source_labels,
                    "Retrieved context": retrieved_context,
                    "Knowledge-base key": result["knowledgebase"],
                    "Failure stage": result["failure_stage"] or "",
                    "Error": result["error"] or "",
                }
            )
        st.download_button(
            "Download all evaluation results as CSV",
            pd.DataFrame(all_table_rows).to_csv(index=False),
            file_name="asklit-evaluation-results.csv",
            mime="text/csv",
        )
        table_rows = [
            row
            for row in all_table_rows
            if (not prompt_filter or row["Prompt"] in prompt_filter)
            and (not model_filter or row["Model"] in model_filter)
            and (not outcome_filter or row["Outcome"] in outcome_filter)
        ]
        st.dataframe(
            pd.DataFrame(table_rows),
            hide_index=True,
            width="stretch",
            column_order=[
                "Outcome",
                "Scenario",
                "Input",
                "Prompt",
                "Knowledge base",
                "Model",
                "Grader",
                "Judge model",
                "Gold label",
                "Answer",
                "Grade",
                "Latency (s)",
                "Approx. tokens",
                "Sources",
                "Error",
            ],
            column_config={
                "Answer": st.column_config.TextColumn(width="large"),
                "Input": st.column_config.TextColumn(width="large"),
                "Gold label": st.column_config.TextColumn(width="medium"),
                "Latency (s)": st.column_config.NumberColumn(format="%.2f"),
            },
        )
        if playground:
            st.success(
                "Ready to keep this project? Choose **4. Export** in the sidebar."
            )


def render_playground_prompt_editor():
    """Render the smallest useful prompt + knowledge-base pairing lesson."""
    st.header("Write a prompt")
    st.write(
        "A prompt tells the assistant how to behave. The knowledge-base name tells "
        "AskLit which uploaded sources this prompt may search. Nothing is published "
        "unless you later choose Export."
    )
    profiles = normalize_prompt_profiles(
        st.session_state.app_config.get("prompt_profiles")
    )
    st.session_state.app_config["prompt_profiles"] = profiles

    selected_index = st.selectbox(
        "Prompt to edit",
        range(len(profiles)),
        format_func=lambda index: profiles[index]["label"],
    )
    profile = profiles[selected_index]
    profile["label"] = st.text_input("Prompt name", profile["label"])
    profile["knowledgebase"] = st.text_input(
        "Knowledge-base name",
        profile["knowledgebase"],
        help="Prompts with the same knowledge-base name search the same uploaded sources.",
    )
    profile["prompt"] = st.text_area(
        "System prompt",
        profile["prompt"],
        height=280,
        help="Describe the assistant's role, audience, boundaries, and desired answer style.",
    )
    profile["key"] = slugify_key(profile["label"], profile["key"])

    action_columns = st.columns(2)
    if action_columns[0].button("Add another prompt"):
        next_number = len(profiles) + 1
        profiles.append(
            {
                "key": f"prompt-{next_number}",
                "label": f"Prompt {next_number}",
                "knowledgebase": profile["knowledgebase"],
                "prompt": profile["prompt"],
                "conversation_starters": [],
                "connected_files": [],
            }
        )
        st.rerun()
    if len(profiles) > 1 and action_columns[1].button("Remove this prompt"):
        profiles.pop(selected_index)
        st.rerun()

    st.info("Next, upload a document to give this prompt a knowledge base.")


def default_scaffold_config(playground=False):
    """Return a complete configuration for a new browser workspace."""
    return {
        "app": {
            "title": "My Knowledge Base",
            "welcome_message": "How can I help you today?",
            "access_mode": "public" if playground else "password",
            "disable_admin": playground,
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
                "prompt": "You are a helpful assistant.",
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


def initialize_scaffold_storage():
    """Create isolated database/vector storage for the current browser session."""
    st.session_state.scaffold_id = str(uuid.uuid4())
    st.session_state.temp_data_dir = os.path.join(
        tempfile.gettempdir(), f"asklit_data_{st.session_state.scaffold_id}"
    )
    os.makedirs(st.session_state.temp_data_dir, exist_ok=True)
    init_db(os.path.join(st.session_state.temp_data_dir, "app.sqlite3"))


def render_workspace_controls(playground):
    """Render YAML save/resume controls shared by both scaffolder modes."""
    with st.sidebar.expander("Save or resume", expanded=False):
        st.caption(
            "Workspace YAML saves settings, prompts, and scenarios—not API keys, "
            "uploaded documents/images, or generated answers."
        )
        workspace_yaml = export_workspace_yaml(
            st.session_state.app_config,
            st.session_state.get("evaluation_scenarios", []),
        )
        st.download_button(
            "Download workspace YAML",
            workspace_yaml,
            file_name="asklit-workspace.yml",
            mime="application/x-yaml",
        )
        uploaded_workspace = st.file_uploader(
            "Import workspace YAML",
            type=["yml", "yaml"],
            key="workspace_yaml_upload",
        )
        if st.button(
            "Import and replace current workspace",
            disabled=uploaded_workspace is None,
        ):
            try:
                imported = import_workspace_yaml(uploaded_workspace.getvalue())
            except (UnicodeDecodeError, ValueError) as exc:
                st.error(str(exc))
            else:
                imported_config = merge_workspace_config(
                    default_scaffold_config(playground), imported["app_config"]
                )
                scenarios = imported["evaluation_scenarios"]
                source_files = imported["source_files_to_reupload"]
                uploaded_assets = imported["uploaded_assets_to_reupload"]
                st.query_params["workspace_mode"] = (
                    "playground" if playground else "builder"
                )
                st.session_state.clear()
                initialize_scaffold_storage()
                st.session_state.app_config = imported_config
                st.session_state.evaluation_scenarios = scenarios
                st.session_state.workspace_source_files = source_files
                st.session_state.workspace_uploaded_assets = uploaded_assets
                st.session_state.workspace_imported = True
                st.rerun()

    if st.session_state.get("workspace_imported"):
        st.sidebar.success("Workspace imported.")
        source_files = st.session_state.get("workspace_source_files", [])
        if source_files:
            st.sidebar.warning(
                "Re-upload these knowledge-base files: " + ", ".join(source_files)
            )
        uploaded_assets = st.session_state.get("workspace_uploaded_assets", [])
        if uploaded_assets:
            st.sidebar.warning(
                "Re-upload these branding images in Builder mode: "
                + ", ".join(uploaded_assets)
            )


def main():
    # Branding: Apply to Scaffolder itself
    logo_url = get_secret_value(
        "branding.logo_url",
        "https://github.com/SuffolkLITLab/logos/raw/main/current-logo/png/lit-lab-logo-large.png",
    )
    homepage_url = get_secret_value(
        "branding.homepage_url", "https://suffolklitlab.org"
    )
    logo_url = safe_url(logo_url)
    homepage_url = safe_url(homepage_url)
    if logo_url:
        st.sidebar.markdown(
            f'<a href="{escape_html(homepage_url)}" target="_blank" rel="noopener noreferrer">'
            f'<img src="{escape_html(logo_url)}" width="150"></a>',
            unsafe_allow_html=True,
        )
        st.sidebar.divider()

    resumed_mode = st.query_params.get("workspace_mode")
    workflow_mode = st.sidebar.radio(
        "Mode",
        ["Playground", "Builder — export an app"],
        index=1 if resumed_mode == "builder" else 0,
        help=(
            "Playground starts with prompt, knowledge-base, and evaluation exercises; "
            "you can still export the finished project."
        ),
    )
    if resumed_mode:
        del st.query_params["workspace_mode"]
    playground = workflow_mode == "Playground"
    if playground:
        st.title("🧪 AskLit Playground")
        st.markdown(
            "Learn how a prompt and knowledge base work together, then evaluate "
            "gold-labeled scenarios. Export the project if you decide to keep it."
        )
    else:
        st.title("🏗️ AskLit Project Scaffolder")
        st.markdown(
            "Configure, test, and bundle a private knowledge-base app into a "
            "GitHub-ready repository."
        )

    if "scaffold_id" not in st.session_state:
        initialize_scaffold_storage()

    if "app_config" not in st.session_state:
        st.session_state.app_config = default_scaffold_config(playground)
    ensure_model_defaults(st.session_state.app_config)
    render_workspace_controls(playground)

    # Sidebar Navigation
    if playground:
        step = st.sidebar.radio(
            "Playground steps",
            ["1. Prompt", "2. Knowledge", "3. Evaluate", "4. Export"],
        )
        step_key = {
            "1. Prompt": "identity",
            "2. Knowledge": "knowledge",
            "3. Evaluate": "experiment",
            "4. Export": "export",
        }[step]
    else:
        step = st.sidebar.radio(
            "Builder steps",
            [
                "1. Identity",
                "2. AI Model",
                "3. Knowledge",
                "4. Experiment Lab",
                "5. Export",
            ],
        )
        step_key = {
            "1. Identity": "identity",
            "2. AI Model": "model",
            "3. Knowledge": "knowledge",
            "4. Experiment Lab": "experiment",
            "5. Export": "export",
        }[step]

    if step_key == "identity" and playground:
        render_playground_prompt_editor()

    elif step_key == "identity":
        st.header("Step 1: App Identity & Branding")

        st.subheader("Basic Information")
        st.session_state.app_config["app"]["title"] = st.text_input(
            "App Title", st.session_state.app_config["app"]["title"]
        )
        st.session_state.app_config["app"]["welcome_message"] = st.text_area(
            "Welcome Message", st.session_state.app_config["app"]["welcome_message"]
        )

        st.subheader("Access Control")
        access_mode = st.selectbox(
            "Who can access the chat?",
            ["Public", "Password Protected"],
            index=(
                0
                if st.session_state.app_config["app"]["access_mode"] == "public"
                else 1
            ),
        )
        st.session_state.app_config["app"]["access_mode"] = (
            "public" if access_mode == "Public" else "password"
        )

        if st.session_state.app_config["app"]["access_mode"] == "password":
            render_password_hash_setup(
                "App access password",
                "scaffold_shared_password_hash",
                "Choose the password visitors will use. AskLit immediately hashes it "
                "and does not retain the plain-text password.",
            )
        else:
            st.session_state.pop("scaffold_shared_password_hash", None)

        st.session_state.app_config["app"]["disable_admin"] = st.checkbox(
            "Disable Admin Backend",
            st.session_state.app_config["app"].get("disable_admin", False),
            help="Hide all management and setup pages in the deployed app.",
        )
        if not st.session_state.app_config["app"]["disable_admin"]:
            render_password_hash_setup(
                "Administrator password",
                "scaffold_admin_password_hash",
                "Choose a separate password for the hidden administration pages. "
                "Only its hash will be placed in deployment secrets.",
            )
        else:
            st.session_state.pop("scaffold_admin_password_hash", None)

        st.subheader("Prompt & Knowledge Base Pairings")
        st.session_state.app_config["prompt_profiles"] = normalize_prompt_profiles(
            st.session_state.app_config.get("prompt_profiles")
        )
        if st.button("Add Prompt Pairing"):
            next_number = len(st.session_state.app_config["prompt_profiles"]) + 1
            st.session_state.app_config["prompt_profiles"].append(
                {
                    "key": f"prompt-{next_number}",
                    "label": f"Prompt {next_number}",
                    "knowledgebase": f"prompt-{next_number}",
                    "prompt": "You are a helpful assistant.",
                    "conversation_starters": [],
                    "connected_files": [],
                }
            )
            st.rerun()

        updated_profiles = []
        for index, profile in enumerate(st.session_state.app_config["prompt_profiles"]):
            with st.expander(profile["label"], expanded=index == 0):
                label = st.text_input(
                    "Navigation Label",
                    profile["label"],
                    key=f"profile_label_{index}",
                )
                key = st.text_input(
                    "YAML Key",
                    profile["key"],
                    key=f"profile_key_{index}",
                    help="Used for the prompt YAML filename and admin overrides.",
                )
                knowledgebase = st.text_input(
                    "Knowledge Base Name",
                    profile["knowledgebase"],
                    key=f"profile_kb_{index}",
                )
                prompt = st.text_area(
                    "System Prompt",
                    profile["prompt"],
                    key=f"profile_prompt_{index}",
                    height=220,
                )
                starters_text = "\n".join(profile.get("conversation_starters", []))
                starters = st.text_area(
                    "Conversation Starters (one per line)",
                    starters_text,
                    key=f"profile_starters_{index}",
                )
                connected_files_text = "\n".join(profile.get("connected_files", []))
                connected_files = st.text_area(
                    "Connected Files",
                    connected_files_text,
                    key=f"profile_files_{index}",
                    help="Optional. Leave blank to connect all files in this knowledge base.",
                )
                remove = len(
                    st.session_state.app_config["prompt_profiles"]
                ) > 1 and st.button("Remove Pairing", key=f"profile_remove_{index}")
                if not remove:
                    updated_profiles.append(
                        {
                            "key": key,
                            "label": label,
                            "knowledgebase": knowledgebase,
                            "prompt": prompt,
                            "conversation_starters": [
                                line.strip()
                                for line in starters.splitlines()
                                if line.strip()
                            ],
                            "connected_files": [
                                line.strip()
                                for line in connected_files.splitlines()
                                if line.strip()
                            ],
                        }
                    )

        st.session_state.app_config["prompt_profiles"] = normalize_prompt_profiles(
            updated_profiles
        )

        st.subheader("Branding Assets")
        # File Uploaders for Branding
        logo_file = st.file_uploader("Upload Logo", type=["png", "jpg", "jpeg", "svg"])
        if logo_file:
            assets_dir = os.path.join(st.session_state.temp_data_dir, "assets")
            os.makedirs(assets_dir, exist_ok=True)
            logo_path = os.path.join(assets_dir, logo_file.name)
            with open(logo_path, "wb") as f:
                f.write(logo_file.getbuffer())
            st.session_state.app_config["branding"]["logo_url"] = (
                f"data/assets/{logo_file.name}"
            )
            st.success(f"Logo uploaded: {logo_file.name}")
        else:
            st.session_state.app_config["branding"]["logo_url"] = st.text_input(
                "Logo URL (fallback)",
                st.session_state.app_config["branding"]["logo_url"],
            )

        favicon_file = st.file_uploader(
            "Upload Favicon", type=["png", "jpg", "ico", "svg"]
        )
        if favicon_file:
            assets_dir = os.path.join(st.session_state.temp_data_dir, "assets")
            os.makedirs(assets_dir, exist_ok=True)
            fav_path = os.path.join(assets_dir, favicon_file.name)
            with open(fav_path, "wb") as f:
                f.write(favicon_file.getbuffer())
            st.session_state.app_config["branding"]["favicon_url"] = (
                f"data/assets/{favicon_file.name}"
            )
            st.success(f"Favicon uploaded: {favicon_file.name}")
        else:
            st.session_state.app_config["branding"]["favicon_url"] = st.text_input(
                "Favicon URL (fallback)",
                st.session_state.app_config["branding"]["favicon_url"],
            )

        st.subheader("Links & Footer")
        st.session_state.app_config["branding"]["homepage_url"] = st.text_input(
            "Homepage URL", st.session_state.app_config["branding"]["homepage_url"]
        )
        st.session_state.app_config["branding"]["supplemental_footer_text"] = (
            st.text_input(
                "Supplemental Footer Text",
                st.session_state.app_config["branding"]["supplemental_footer_text"],
                help="Appears before the 'Made with AskLit' link.",
            )
        )
        st.session_state.app_config["branding"]["hide_asklit_badge"] = st.checkbox(
            "Hide 'Made with AskLit' link",
            st.session_state.app_config["branding"]["hide_asklit_badge"],
        )

    elif step_key == "model":
        st.header("Step 2: AI Configuration")
        st.info(
            "You won't provide API keys here. The scaffolder's own credentials are "
            "never exported; add your key later in the generated app's private "
            "Streamlit settings."
        )

        previous_provider = st.session_state.app_config["model"].get(
            "provider", "openai"
        )
        provider = st.selectbox(
            "LLM Provider",
            ["openai", "azure_apim", "anthropic", "google", "groq", "mistral"],
            index=[
                "openai",
                "azure_apim",
                "anthropic",
                "google",
                "groq",
                "mistral",
            ].index(previous_provider),
            help="Azure APIM uses a limited gateway credential instead of exposing a Foundry account key.",
        )
        current_model = st.session_state.app_config["model"].get(
            "name", "gpt-5.4-mini" if provider == "openai" else ""
        )
        current_allowed_models = st.session_state.app_config["model"].get(
            "allowed_models", ""
        )
        current_base_url = (
            st.session_state.app_config["model"].get("base_url", "")
            if provider == previous_provider
            else ""
        )
        custom_openai_endpoint = False
        custom_apim_endpoint = False
        model_base_url = ""
        endpoint_error = None
        if provider == "openai":
            custom_openai_endpoint = st.checkbox(
                "Use a custom OpenAI-compatible endpoint",
                value=bool(current_base_url),
                help=(
                    "Examples include an OpenAI-compatible proxy or Azure's "
                    "/openai/v1 endpoint. The generated app will use your own API key."
                ),
            )
            if custom_openai_endpoint:
                entered_base_url = st.text_input(
                    "OpenAI-compatible base URL",
                    value=current_base_url,
                    placeholder="https://example.com/v1",
                )
                model_base_url, endpoint_error = normalize_openai_base_url(
                    entered_base_url
                )
                if endpoint_error:
                    st.error(endpoint_error)
                elif model_base_url.startswith("http://"):
                    st.warning(
                        "Use HTTPS for a remotely hosted app. HTTP should be limited "
                        "to local development endpoints."
                    )
                st.caption(
                    "For safety, this public scaffolder does not send its API key to "
                    "custom endpoints. Enter the model name manually; the exported app "
                    "will use this URL with the API key you add to its secrets."
                )
        elif provider == "azure_apim":
            trusted_gateway_url = str(get_base_url("azure_apim") or "").rstrip("/")
            entered_gateway_url = st.text_input(
                "Azure APIM gateway base URL",
                value=current_base_url or trusted_gateway_url,
                placeholder="https://your-gateway.azure-api.net/asklit",
                help=(
                    "The current workshop gateway is prefilled. Change it only when "
                    "the generated app should use a different APIM gateway."
                ),
            )
            model_base_url, endpoint_error = normalize_openai_base_url(
                entered_gateway_url
            )
            custom_apim_endpoint = bool(
                entered_gateway_url
                and (
                    endpoint_error
                    or not trusted_gateway_url
                    or model_base_url.rstrip("/") != trusted_gateway_url
                )
            )
            if endpoint_error:
                st.error(endpoint_error)
            elif custom_apim_endpoint:
                st.caption(
                    "This URL will be exported, but the scaffolder will not send its "
                    "gateway key to an untrusted custom gateway. Enter deployment "
                    "names manually."
                )
            else:
                st.caption("Using the scaffolder's current default APIM gateway URL.")

        configured_for_provider = (
            [current_model, *parse_model_names(current_allowed_models)]
            if provider == previous_provider
            else []
        )
        manual_custom_endpoint = custom_openai_endpoint or custom_apim_endpoint
        if manual_custom_endpoint:
            model_choices, choice_source, discovery = [], None, None
        else:
            model_choices, choice_source, discovery = get_endpoint_model_choices(
                provider,
                configured_for_provider,
                base_url_override=(
                    model_base_url if provider == "azure_apim" else None
                ),
            )
            render_endpoint_model_status(discovery, choice_source)

        if manual_custom_endpoint:
            model_name = st.text_input("Model Name", value=current_model)
        elif model_choices:
            selected_index = (
                model_choices.index(current_model)
                if current_model in model_choices
                else 0
            )
            model_name = st.selectbox(
                "Model Name",
                model_choices,
                index=selected_index,
            )
        else:
            model_name = st.text_input("Model Name", value=current_model)

        allow_user_selection = False
        allowed_models = current_allowed_models
        if provider == "azure_apim":
            allow_user_selection = st.checkbox(
                "Let users choose among approved models",
                value=st.session_state.app_config["model"].get(
                    "allow_user_selection", True
                ),
            )
            allowed_models = st.text_area(
                "Approved Azure deployment names",
                value=st.session_state.app_config["model"].get(
                    "allowed_models",
                    "gpt-5.4-nano,gpt-5.4-mini,gpt-5.6-sol,deepseek-v4-pro,grok-4.1-fast-reasoning,llama-4-maverick,kimi-k2.6,mistral-large-3,phi-4-mini,gpt-4.1-nano,gpt-4.1-mini",
                ),
                help="Comma-separated. Keep this synchronized with the APIM policy allowlist.",
            )

        st.session_state.app_config["model"] = {
            "provider": provider,
            "name": model_name,
            "allow_user_selection": allow_user_selection,
            "allowed_models": allowed_models,
            "base_url": model_base_url,
            "use_local_embeddings": True,
            "local_embedding_model": "all-MiniLM-L6-v2",
        }
        st.success("Using local embeddings (no API key needed during scaffolding!)")

    elif step_key == "knowledge":
        st.header("Add a knowledge base" if playground else "Step 3: Upload Knowledge")
        st.write("Upload the PDFs or documents you want your AI to know about.")
        st.session_state.app_config["prompt_profiles"] = normalize_prompt_profiles(
            st.session_state.app_config.get("prompt_profiles")
        )
        profile_labels = [
            f"{profile['label']} ({profile['knowledgebase']})"
            for profile in st.session_state.app_config["prompt_profiles"]
        ]
        selected_profile_index = st.selectbox(
            "Attach uploaded files to",
            range(len(profile_labels)),
            format_func=lambda index: profile_labels[index],
        )
        selected_profile = st.session_state.app_config["prompt_profiles"][
            selected_profile_index
        ]

        uploaded_files = st.file_uploader(
            "Upload Documents",
            accept_multiple_files=True,
            type=["pdf", "docx", "txt", "md"],
        )

        if uploaded_files:
            if st.button("Process & Index Documents"):
                with st.spinner("Chunking and Embedding... (this may take a minute)"):
                    chroma_path = os.path.join(st.session_state.temp_data_dir, "chroma")
                    db_path = os.path.join(
                        st.session_state.temp_data_dir, "app.sqlite3"
                    )

                    uploads_dir = os.path.join(
                        st.session_state.temp_data_dir, "uploads"
                    )
                    os.makedirs(uploads_dir, exist_ok=True)

                    for uploaded_file in uploaded_files:
                        file_id = str(uuid.uuid4())
                        ext = os.path.splitext(uploaded_file.name)[1]
                        file_path = os.path.join(uploads_dir, f"{file_id}{ext}")

                        with open(file_path, "wb") as f:
                            f.write(uploaded_file.getbuffer())

                        full_text, pages = extract_text(file_path)
                        content_hash = get_content_hash(full_text)
                        chunks = chunk_pages(pages)

                        # Add to SQLite
                        conn = get_connection(db_path=db_path)
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT INTO documents (id, knowledgebase, filename, file_path, file_type, file_size, content_hash, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                file_id,
                                selected_profile["knowledgebase"],
                                uploaded_file.name,
                                f"data/uploads/{file_id}{ext}",
                                ext,
                                uploaded_file.size,
                                content_hash,
                                "indexed",
                            ),
                        )
                        chunk_data = [
                            (file_id, c["chunk_index"], c["content"], c["page_number"])
                            for c in chunks
                        ]
                        cursor.executemany(
                            "INSERT INTO document_chunks (document_id, chunk_index, content, page_number) VALUES (?, ?, ?, ?)",
                            chunk_data,
                        )
                        conn.commit()
                        conn.close()

                        # Add to Chroma
                        add_document_to_index(
                            file_id,
                            chunks,
                            chroma_path=chroma_path,
                            knowledgebase=selected_profile["knowledgebase"],
                        )
                        if (
                            uploaded_file.name
                            not in st.session_state.app_config["prompt_profiles"][
                                selected_profile_index
                            ]["connected_files"]
                        ):
                            st.session_state.app_config["prompt_profiles"][
                                selected_profile_index
                            ]["connected_files"].append(uploaded_file.name)

                    st.success(f"Indexed {len(uploaded_files)} documents!")

    elif step_key == "experiment":
        render_experiment_lab(playground=playground)

    elif step_key == "export":
        ensure_model_defaults(st.session_state.app_config)
        st.header(
            "Export your project" if playground else "Step 5: Export your Project"
        )
        if playground:
            st.info(
                "Playground projects start with public access and the admin backend "
                "disabled. Switch to Builder first if you want passwords, branding, "
                "or detailed deployment settings."
            )

        # 1. Configuration Generator
        with st.expander("📋 Deployment Settings & Secrets", expanded=True):
            st.markdown(
                "Paste the following into your **Streamlit Cloud > Settings > Secrets** panel:"
            )

            password_hashes = {
                "shared": st.session_state.get("scaffold_shared_password_hash"),
                "admin": st.session_state.get("scaffold_admin_password_hash"),
            }
            secrets_toml = generate_deployment_secrets(
                st.session_state.app_config,
                password_hashes=password_hashes,
            )

            missing_passwords = []
            if (
                st.session_state.app_config["app"]["access_mode"] == "password"
                and not password_hashes["shared"]
            ):
                missing_passwords.append("app access password")
            if (
                not st.session_state.app_config["app"].get("disable_admin")
                and not password_hashes["admin"]
            ):
                missing_passwords.append("administrator password")
            if missing_passwords:
                st.warning(
                    "Return to Identity and configure: "
                    + ", ".join(missing_passwords)
                    + "."
                )

            st.code(secrets_toml, language="toml")
            st.download_button(
                "Download deployment secrets",
                data=secrets_toml,
                file_name="asklit-deployment-secrets.toml",
                mime="text/plain",
                help=(
                    "This contains password hashes and an API-key placeholder. "
                    "Keep it out of GitHub."
                ),
            )

        st.divider()
        st.subheader("Your no-code deployment checklist")
        st.markdown(
            """
1. Click **Connect to GitHub** below and approve AskLit.
2. Create the repository and wait for **Finished publishing files.**
3. Open [Streamlit Community Cloud](https://share.streamlit.io/) and sign in with the same GitHub account.
4. Click **Create app**, choose the new repository, branch **main**, and file **app.py**.
5. In **Advanced settings**, paste your real configuration into **Secrets**, then click **Deploy**.

Never upload `secrets.toml` to GitHub. Streamlit's **Secrets** box is the only place for real keys.
"""
        )
        repo_name = st.text_input("Repository Name", DEFAULT_REPO_NAME)
        private_repo = st.checkbox(
            "Make the GitHub repository private",
            value=False,
            help=(
                "Public repositories work with Streamlit Community Cloud's free "
                "deployment path. Choose private only if your hosting account can "
                "access private repositories."
            ),
        )
        if not private_repo:
            st.warning(
                "This repository will be public. Its generated configuration and "
                "knowledge-base documents will be visible to everyone. Do not publish "
                "confidential, student, client, or secret material."
            )

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Option A: Download ZIP")
            st.write(
                "Download the complete project folder, ready to be uploaded to GitHub manually."
            )
            if st.button("Prepare ZIP Download"):
                with st.spinner("Bundling files..."):
                    bundle_dir = create_bundle(
                        st.session_state.app_config, st.session_state.temp_data_dir
                    )
                    zip_path = os.path.join(tempfile.gettempdir(), f"{repo_name}.zip")
                    zip_directory(bundle_dir, zip_path)

                    with open(zip_path, "rb") as f:
                        st.download_button(
                            label="📥 Download Project ZIP",
                            data=f,
                            file_name=f"{repo_name}.zip",
                            mime="application/zip",
                        )

        with col2:
            st.subheader("Option B: Push to GitHub")

            client_id = get_secret_value("GITHUB_CLIENT_ID", None)

            if not client_id:
                st.error(
                    "The centralized GitHub connection is temporarily unavailable. "
                    "Please contact the AskLit administrator; you should never need to create a personal access token."
                )
                github_token = None
            else:
                github_token = st.session_state.get("github_oauth_token")
                device = st.session_state.get("github_oauth_device")

                if device and time.time() >= device["expires_at"]:
                    del st.session_state.github_oauth_device
                    device = None
                    st.warning("The GitHub connection code expired. Start again below.")

                if not github_token and not device:
                    if st.button("🔗 Connect to GitHub", type="primary"):
                        try:
                            device = request_device_code(
                                client_id, timeout=REQUEST_TIMEOUT_SECONDS
                            )
                            device["expires_at"] = time.time() + int(
                                device["expires_in"]
                            )
                            st.session_state.github_oauth_device = device
                            st.rerun()
                        except GitHubError as exc:
                            st.error(str(exc))

                if not github_token and device:
                    st.info(
                        "Open GitHub, enter the one-time code below, and approve "
                        "private-repository access. Keep this page open."
                    )
                    st.code(device["user_code"], language=None)
                    st.link_button(
                        "Open GitHub authorization",
                        device["verification_uri"],
                        type="primary",
                    )
                    if st.button("I've authorized GitHub — connect"):
                        try:
                            result = poll_device_token(
                                client_id,
                                device["device_code"],
                                timeout=REQUEST_TIMEOUT_SECONDS,
                            )
                            if result["status"] == "complete":
                                st.session_state.github_oauth_token = result[
                                    "access_token"
                                ]
                                st.session_state.pop("github_oauth_device", None)
                                st.rerun()
                            elif result["status"] == "pending":
                                st.info(
                                    "GitHub has not received the approval yet. "
                                    "Approve it there, then try this button again."
                                )
                            else:
                                st.session_state.pop("github_oauth_device", None)
                                st.error(result["message"])
                        except GitHubError as exc:
                            st.error(str(exc))

                if github_token:
                    try:
                        github_user = st.session_state.get("github_oauth_user")
                        if not github_user:
                            github_user = get_authenticated_user(
                                github_token, timeout=REQUEST_TIMEOUT_SECONDS
                            )
                            st.session_state.github_oauth_user = github_user
                        st.success(f"Connected to GitHub as {github_user['login']}.")
                    except GitHubError as exc:
                        st.session_state.pop("github_oauth_token", None)
                        st.session_state.pop("github_oauth_user", None)
                        github_token = None
                        st.error(str(exc))
                    if github_token and st.button("Disconnect GitHub"):
                        st.session_state.pop("github_oauth_token", None)
                        st.session_state.pop("github_oauth_user", None)
                        st.rerun()

            if github_token:
                if st.button("🚀 Create Repo & Push"):
                    with st.spinner("Creating repository..."):
                        bundle_dir = create_bundle(
                            st.session_state.app_config,
                            st.session_state.temp_data_dir,
                        )
                        publish_progress = st.progress(0.0, text="Preparing files...")

                        def update_publish_progress(completed, total, path):
                            publish_progress.progress(
                                completed / total,
                                text=f"Publishing {path} ({completed}/{total})",
                            )

                        try:
                            result = publish_directory(
                                github_token,
                                repo_name,
                                bundle_dir,
                                private=private_repo,
                                timeout=REQUEST_TIMEOUT_SECONDS,
                                progress=update_publish_progress,
                            )
                            publish_progress.progress(
                                1.0, text="Finished publishing files."
                            )
                            st.success(
                                f"Published {result['files_published']} files to "
                                f"{result['full_name']}."
                            )
                            st.link_button(
                                "Open the new GitHub repository",
                                result["html_url"],
                                type="primary",
                            )
                        except GitHubError as exc:
                            st.error(str(exc))
                            st.caption(
                                "If GitHub created the repository before an upload "
                                "failed, open GitHub to review or delete the partial repository."
                            )


if __name__ == "__main__":
    main()
