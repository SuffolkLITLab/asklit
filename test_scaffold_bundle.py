from pathlib import Path

import scaffold


def test_production_runtime_entrypoint_has_no_scaffolder_page_reference():
    runtime_app = Path(scaffold.__file__).parent / "runtime" / "app.py"

    assert runtime_app.exists()
    assert "scaffold.py" not in runtime_app.read_text(encoding="utf-8")


def test_workspace_yaml_round_trip_excludes_secrets_and_binary_state():
    workspace_yaml = scaffold.export_workspace_yaml(
        {
            "app": {"title": "Class project"},
            "model": {"name": "test-model", "api_key": "must-not-serialize"},
            "branding": {
                "logo_url": "data/assets/uploaded-logo.png",
                "homepage_url": "https://example.org",
            },
            "prompt_profiles": [
                {
                    "key": "housing",
                    "label": "Housing",
                    "knowledgebase": "housing",
                    "prompt": "Use the housing guide.",
                    "connected_files": ["housing-guide.pdf"],
                }
            ],
        },
        [
            {
                "input": "What should I do?",
                "__expected": "icontains:notice",
                "__description": "Notice",
            }
        ],
    )

    assert "must-not-serialize" not in workspace_yaml
    imported = scaffold.import_workspace_yaml(workspace_yaml)
    assert imported["app_config"]["app"]["title"] == "Class project"
    assert imported["app_config"]["prompt_profiles"][0]["connected_files"] == []
    assert imported["source_files_to_reupload"] == ["housing-guide.pdf"]
    assert imported["uploaded_assets_to_reupload"] == ["uploaded-logo.png"]
    assert "logo_url" not in imported["app_config"]["branding"]
    assert imported["evaluation_scenarios"][0]["input"] == "What should I do?"


def test_workspace_yaml_rejects_unknown_schema():
    try:
        scaffold.import_workspace_yaml(
            "asklit_workspace:\n  schema_version: 999\n  app_config: {}\n"
        )
    except ValueError as exc:
        assert "version" in str(exc).lower()
    else:
        raise AssertionError("Expected an unknown workspace schema to be rejected")


def test_create_bundle_recursively_excludes_local_artifacts(monkeypatch, tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    for source_name in scaffold.RUNTIME_ROOT_FILES:
        source_path = source_root / source_name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"# {source_name}\n", encoding="utf-8")
    admin = source_root / "admin"
    admin.mkdir()
    (admin / "settings.py").write_text("# admin\n", encoding="utf-8")
    package = source_root / "asklit"
    package.mkdir()
    for filename in scaffold.RUNTIME_ASKLIT_MODULES:
        (package / filename).write_text(f"# {filename}\n", encoding="utf-8")
    (package / "github.py").write_text("# scaffolder only\n", encoding="utf-8")
    (package / "experiments.py").write_text("# scaffolder only\n", encoding="utf-8")
    (package / "models.py").write_text("# scaffolder only\n", encoding="utf-8")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "config.pyc").write_bytes(b"compiled")
    (source_root / ".pytest_cache").mkdir()
    (source_root / ".pytest_cache" / "state").write_text("cache", encoding="utf-8")
    (source_root / ".venv").mkdir()
    (source_root / ".venv" / "python").write_text("binary", encoding="utf-8")
    (source_root / "output").mkdir()
    (source_root / "output" / "result.json").write_text("{}", encoding="utf-8")
    (source_root / "tmp").mkdir()
    (source_root / "tmp" / "debug.txt").write_text("debug", encoding="utf-8")
    (source_root / "scaffold.py").write_text("# scaffolder\n", encoding="utf-8")
    (source_root / "test_runtime.py").write_text("# test\n", encoding="utf-8")
    (source_root / "Dockerfile").write_text("FROM python\n", encoding="utf-8")
    (source_root / "fly.toml").write_text("app = 'wrong'\n", encoding="utf-8")
    (source_root / ".env").write_text(
        "OPENAI_API_KEY=host-key-must-not-export\n"
        "AZURE_APIM_API_KEY=gateway-key-must-not-export\n",
        encoding="utf-8",
    )
    (source_root / ".streamlit").mkdir()
    (source_root / ".streamlit" / "secrets.toml").write_text(
        'OPENAI_API_KEY = "host-key-must-not-export"\n'
        'AZURE_APIM_API_KEY = "gateway-key-must-not-export"\n',
        encoding="utf-8",
    )
    (source_root / "docs").mkdir()
    (source_root / "docs" / "operator.md").write_text("docs", encoding="utf-8")

    session_data = tmp_path / "session-data"
    session_data.mkdir()
    scaffold.init_db(str(session_data / "app.sqlite3"))
    monkeypatch.setattr(scaffold, "__file__", str(source_root / "scaffold.py"))

    bundle = Path(
        scaffold.create_bundle(
            {
                "app": {"title": "Generated App"},
                "model": {
                    "provider": "openai",
                    "name": "custom-model",
                    "base_url": "https://models.example/v1",
                    "api_key": "host-key-must-not-export",
                },
                "prompt_profiles": [],
            },
            str(session_data),
        )
    )

    assert (bundle / "app.py").exists()
    assert (bundle / "chat_ui.py").exists()
    assert (bundle / "admin" / "settings.py").exists()
    assert (bundle / "asklit" / "config.py").exists()
    assert (bundle / "data" / "app.sqlite3").exists()
    assert not list((bundle / "data").glob("*-wal"))
    assert not list((bundle / "data").glob("*-shm"))
    assert (bundle / "prompts" / "default.yml").exists()
    assert not list(bundle.rglob("__pycache__"))
    assert not list(bundle.rglob("*.pyc"))
    assert not (bundle / ".pytest_cache").exists()
    assert not (bundle / ".venv").exists()
    assert not (bundle / "output").exists()
    assert not (bundle / "tmp").exists()
    assert not (bundle / "scaffold.py").exists()
    assert not (bundle / "test_runtime.py").exists()
    assert not (bundle / "Dockerfile").exists()
    assert not (bundle / "fly.toml").exists()
    assert not (bundle / "docs").exists()
    assert not (bundle / "asklit" / "github.py").exists()
    assert not (bundle / "asklit" / "experiments.py").exists()
    assert not (bundle / "asklit" / "models.py").exists()
    assert {path.name for path in bundle.iterdir()} == {
        ".gitignore",
        ".streamlit",
        "README.md",
        "admin",
        "app.py",
        "asklit",
        "chat_ui.py",
        "config",
        "data",
        "login_ui.py",
        "prompts",
        "requirements.txt",
    }
    assert {path.name for path in (bundle / "asklit").iterdir()} == set(
        scaffold.RUNTIME_ASKLIT_MODULES
    )
    assert "scaffold.py" not in (bundle / "app.py").read_text(encoding="utf-8")
    assert "maxUploadSize = 10" in (bundle / ".streamlit" / "config.toml").read_text(
        encoding="utf-8"
    )
    generated_config = scaffold.toml.load(bundle / "config" / "defaults.toml")
    assert generated_config["model"]["base_url"] == "https://models.example/v1"
    assert "api_key" not in generated_config["model"]
    for path in bundle.rglob("*"):
        if path.is_file():
            assert b"host-key-must-not-export" not in path.read_bytes()
            assert b"gateway-key-must-not-export" not in path.read_bytes()


def test_deployment_secrets_include_custom_openai_endpoint():
    output = scaffold.generate_deployment_secrets(
        {
            "app": {
                "title": "Custom Endpoint",
                "access_mode": "public",
                "disable_admin": True,
            },
            "model": {
                "provider": "openai",
                "name": "custom-model",
                "base_url": "https://models.example/v1",
            },
        }
    )

    assert 'OPENAI_BASE_URL = "https://models.example/v1"' in output
    assert 'OPENAI_API_KEY = "PASTE_YOUR_KEY_HERE"' in output


def test_deployment_secrets_include_generated_hashes_without_plain_passwords():
    shared_hash = scaffold.hash_password("shared password")
    admin_hash = scaffold.hash_password("admin password")
    output = scaffold.generate_deployment_secrets(
        {
            "app": {
                "title": "Protected App",
                "access_mode": "password",
                "disable_admin": False,
            },
            "model": {"provider": "openai", "name": "gpt-5.4-mini"},
        },
        password_hashes={"shared": shared_hash, "admin": admin_hash},
    )

    assert f'SHARED_PASSWORD_HASH = "{shared_hash}"' in output
    assert f'ADMIN_PASSWORD_HASH = "{admin_hash}"' in output
    assert "shared password" not in output
    assert "admin password" not in output
    assert "PASTE_SHARED_HASH_HERE" not in output
    assert "PASTE_ADMIN_HASH_HERE" not in output


def test_deployment_secrets_include_selected_apim_gateway_url():
    output = scaffold.generate_deployment_secrets(
        {
            "app": {
                "title": "Gateway App",
                "access_mode": "public",
                "disable_admin": True,
            },
            "model": {
                "provider": "azure_apim",
                "name": "gpt-5.4-mini",
                "base_url": "https://custom-gateway.azure-api.net/asklit",
                "allowed_models": "gpt-5.4-mini",
                "allow_user_selection": True,
            },
        }
    )

    assert (
        'AZURE_APIM_BASE_URL = "https://custom-gateway.azure-api.net/asklit"' in output
    )
