"""Build and package the deployable chatbot runtime from a workspace."""

import os
import shutil
import tempfile
import zipfile

import toml
import yaml

from asklit.db import get_connection
from asklit.scaffold.config import normalize_prompt_profiles
from asklit.scaffold.workspace import sanitize_export_config

DEFAULT_REPO_NAME = "my-asklit-app"
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


def ignore_bundle_artifacts(_directory, names):
    """Filter generated/runtime artifacts at every depth of a scaffold copy."""
    return [
        name
        for name in names
        if name in RECURSIVE_ARTIFACT_NAMES
        or name.endswith(RECURSIVE_ARTIFACT_SUFFIXES)
    ]


def create_bundle(config_data, data_dir_source):
    """Create a deployable chatbot runtime with generated config and data."""
    temp_dir = tempfile.mkdtemp()
    root_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

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
