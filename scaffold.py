import streamlit as st
import os
import shutil
import tempfile
import zipfile
import toml
import uuid
import yaml
import time
from asklit.auth import hash_password
from asklit.db import get_connection, init_db
from asklit.experiments import (
    build_experiment_matrix,
    build_experiment_messages,
    parse_model_names,
    response_text,
)
from asklit.ingestion import extract_text, chunk_pages, get_content_hash
from asklit.github import (
    GitHubError,
    get_authenticated_user,
    poll_device_token,
    publish_directory,
    request_device_code,
)
from asklit.llm import call_llm, estimate_tokens
from asklit.models import (
    choose_model_options,
    discover_available_models,
    normalize_openai_base_url,
)
from asklit.observability import log_ai_call_event
from asklit.rag import add_document_to_index, query_index
from asklit.config import get_api_key, get_base_url, get_secret_value, get_setting
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
RECURSIVE_ARTIFACT_SUFFIXES = (".pyc", ".pyo")
SENSITIVE_EXPORT_CONFIG_KEYS = {
    "api_key",
    "client_secret",
    "access_token",
    "password",
    "password_hash",
    "shared_password_hash",
    "admin_password_hash",
}


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
            if normalized_key in SENSITIVE_EXPORT_CONFIG_KEYS or normalized_key.endswith(
                "_api_key"
            ):
                continue
            sanitized[key] = sanitize_export_config(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_export_config(item) for item in value]
    return value


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
        base_url_override
        if base_url_override is not None
        else get_base_url(provider)
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

    # 3. Copy the built data directory (SQLite + Chroma)
    dst_data_dir = os.path.join(temp_dir, "data")
    if os.path.exists(dst_data_dir):
        shutil.rmtree(dst_data_dir)
    shutil.copytree(data_dir_source, dst_data_dir)

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
            "[server]\nheadless = true\nenableCORS = false\nenableXsrfProtection = false\n"
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
            f'OPENAI_BASE_URL = {toml_quote(config_data["model"]["base_url"])}'
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


def render_experiment_lab():
    """Render a small Cartesian prompt/knowledge-base/model comparison lab."""
    ensure_model_defaults(st.session_state.app_config)
    profiles = normalize_prompt_profiles(
        st.session_state.app_config.get("prompt_profiles")
    )
    st.session_state.app_config["prompt_profiles"] = profiles
    model_config = st.session_state.app_config["model"]

    st.header("Step 4: Experiment Lab")
    st.write(
        "Ask one question across different prompts, knowledge bases, and models. "
        "Experiments are temporary and are not included in the exported app."
    )
    st.warning(
        "Each combination makes a real model call using credentials configured for this scaffolder. "
        "Your provider may charge for these calls."
    )

    labels = {profile["key"]: profile["label"] for profile in profiles}
    prompt_keys = st.multiselect(
        "Prompts",
        [profile["key"] for profile in profiles],
        default=[profiles[0]["key"]],
        format_func=lambda key: labels[key],
        help="The system prompt to place at the beginning of each test request.",
    )
    knowledgebase_keys = st.multiselect(
        "Knowledge bases",
        [profile["key"] for profile in profiles],
        default=[profiles[0]["key"]],
        format_func=lambda key: (
            f"{labels[key]} ({next(profile['knowledgebase'] for profile in profiles if profile['key'] == key)})"
        ),
        help="Uses the knowledge base and connected-file filters from the selected pairing.",
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
    if model_choices:
        default_models = (
            [configured_model]
            if configured_model in model_choices
            else model_choices[:1]
        )
        model_names = st.multiselect(
            "Models",
            model_choices,
            default=default_models,
            help="Select one or more models to compare.",
        )
    else:
        model_names = st.text_area(
            "Models (one per line or comma-separated)",
            value=configured_model,
            help="Use model or deployment names accepted by the selected provider.",
        )
    question = st.text_area(
        "Test question",
        placeholder="What should a user know about…?",
    )
    top_k = st.slider("Retrieved passages per run", 1, 10, 5)

    matrix = build_experiment_matrix(
        profiles, prompt_keys, knowledgebase_keys, model_names
    )
    run_count = len(matrix)
    if run_count:
        st.caption(f"{run_count} model call{'s' if run_count != 1 else ''} will run.")
    if run_count > 12:
        st.error("Choose fewer options so the experiment has no more than 12 runs.")

    can_run = bool(question.strip() and matrix and run_count <= 12 and provider_ready)
    if st.button("Run experiment", type="primary", disabled=not can_run):
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
            progress.progress(
                index / run_count,
                text=f"Running {prompt_profile['label']} × {knowledgebase_profile['label']} × {model}",
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

            results.append(
                {
                    "run_id": experiment_run_id,
                    "prompt_label": prompt_profile["label"],
                    "knowledgebase_label": knowledgebase_profile["label"],
                    "knowledgebase": knowledgebase_profile["knowledgebase"],
                    "model": model,
                    "provider": provider,
                    "answer": answer,
                    "error": safe_error,
                    "failure_stage": failure_stage,
                    "elapsed": elapsed,
                    "tokens": estimate_tokens(question) + estimate_tokens(answer),
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
        st.session_state.experiment_question = question

    results = st.session_state.get("experiment_results", [])
    if results:
        st.subheader("Results")
        st.caption(f"Question: {st.session_state.get('experiment_question', question)}")
        columns = st.columns(min(3, len(results)))
        for index, result in enumerate(results):
            with columns[index % len(columns)]:
                st.markdown(
                    f"#### {result['prompt_label']} × {result['knowledgebase_label']}"
                )
                st.caption(
                    f"{result['model']} via {result['provider']} · "
                    f"{result['elapsed']:.1f}s · ~{result['tokens']} tokens · "
                    f"run {result['run_id'][:8]}"
                )
                if result["error"]:
                    st.error(
                        f"{result['failure_stage'].title()} failed: {result['error']}"
                    )
                else:
                    st.markdown(result["answer"])
                with st.expander(f"Retrieved sources ({len(result['sources'])})"):
                    if not result["sources"]:
                        st.write("No matching passages were retrieved.")
                    for source_index, source in enumerate(result["sources"]):
                        page = source["page"] if source["page"] is not None else "N/A"
                        st.markdown(
                            f"**Source {source_index + 1}: {source['filename']}, page {page}**"
                        )
                        st.write(source["content"])
                        st.divider()


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

    st.title("🏗️ AskLit Project Scaffolder")
    st.markdown("""
    Create your own private AI Knowledge Base app in minutes. 
    This tool will help you configure your app, upload your documents, 
    and bundle everything into a GitHub-ready repository.
    """)

    if "scaffold_id" not in st.session_state:
        st.session_state.scaffold_id = str(uuid.uuid4())
        # Create a unique data directory for this session's build
        st.session_state.temp_data_dir = os.path.join(
            tempfile.gettempdir(), f"asklit_data_{st.session_state.scaffold_id}"
        )
        os.makedirs(st.session_state.temp_data_dir, exist_ok=True)
        # Initialize a fresh DB in the temp dir
        db_path = os.path.join(st.session_state.temp_data_dir, "app.sqlite3")
        init_db(db_path)

    if "app_config" not in st.session_state:
        st.session_state.app_config = {
            "app": {
                "title": "My Knowledge Base",
                "welcome_message": "How can I help you today?",
                "access_mode": "password",
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
    ensure_model_defaults(st.session_state.app_config)

    # Sidebar Navigation
    step = st.sidebar.radio(
        "Steps",
        [
            "1. Identity",
            "2. AI Model",
            "3. Knowledge",
            "4. Experiment Lab",
            "5. Export",
        ],
    )

    if step == "1. Identity":
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

    elif step == "2. AI Model":
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

    elif step == "3. Knowledge":
        st.header("Step 3: Upload Knowledge")
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

    elif step == "4. Experiment Lab":
        render_experiment_lab()

    elif step == "5. Export":
        ensure_model_defaults(st.session_state.app_config)
        st.header("Step 5: Export your Project")

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
