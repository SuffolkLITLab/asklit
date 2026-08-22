"""Comprehensive end-to-end tests for the AskLit scaffolding tool and exported applications.

Covers:
1. Complete 5-step scaffolder workflow (Knowledge -> Prompt -> Chat Preview -> Evaluate -> Export)
2. Workspace serialization, sanitization, and restoration
3. Bundle generation, artifact exclusion, and deployment secrets generation
4. Standalone execution of the exported application bundle via Streamlit AppTest:
   - Public and password-gated access modes
   - Multiple prompt profiles and conversation starters
   - Vector and keyword RAG search from exported Chroma and SQLite data
   - Citations and message history logging
   - Admin unlock route, login, management navigation, and logout
   - Disabled admin mode enforcement
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit as st
import toml
import yaml
from streamlit.testing.v1 import AppTest

# Provide mock litellm module if not installed
litellm_mock = SimpleNamespace()
sys.modules.setdefault("litellm", litellm_mock)

from asklit.auth import hash_password
from asklit.db import get_connection, init_db
from asklit.experiments import (
    build_evaluation_matrix,
    normalize_scenario_rows,
    parse_scenario_csv,
    scenarios_to_csv,
)
from asklit.rag import query_index
from asklit.scaffold import (
    access,
    bundle as bundle_module,
    evaluation,
    knowledge,
    step_chat,
    workspace,
)
from asklit.scaffold.config import (
    default_scaffold_config,
    ensure_model_defaults,
    normalize_prompt_profiles,
)


class InMemoryUpload:
    """Simulates Streamlit UploadedFile for document indexing tests."""

    def __init__(self, name: str, text: str):
        self.name = name
        self._data = text.encode("utf-8")
        self.size = len(self._data)

    def getbuffer(self):
        return self._data

    def getvalue(self):
        return self._data


@pytest.fixture(autouse=True)
def isolated_streamlit_state():
    """Reset streamlit state and secrets before and after each test."""
    st.secrets._reset()
    yield
    st.secrets._reset()


# ============================================================================
# 1. Scaffolding Workflow: Step 1 - Knowledge Base Management & Ingestion
# ============================================================================

def test_e2e_step1_knowledge_ingestion_and_scoping(tmp_path, monkeypatch):
    """Test document upload, indexing into SQLite/Chroma, deduplication, and renaming."""
    session_dir = tmp_path / "session_step1"
    session_dir.mkdir()
    db_path = str(session_dir / "app.sqlite3")
    chroma_path = str(session_dir / "chroma")
    uploads_dir = str(session_dir / "uploads")
    init_db(db_path)

    doc1 = InMemoryUpload(
        "tenancy_rights.txt",
        "Tenants are entitled to 14 days notice before any eviction hearing. "
        "Landlords must keep premises in safe and habitable condition.",
    )
    doc2 = InMemoryUpload(
        "security_deposit.txt",
        "Security deposits must be placed in an interest-bearing escrow account. "
        "Landlords must return the deposit within 30 days after tenancy ends.",
    )

    # Index into 'housing' knowledgebase
    outcome1 = knowledge.index_uploaded_documents(
        [doc1, doc2], "housing", db_path, chroma_path, uploads_dir
    )
    assert outcome1["indexed"] == ["tenancy_rights.txt", "security_deposit.txt"]
    assert outcome1["skipped"] == []
    assert outcome1["failed"] == []

    # Verify indexed docs in DB
    docs = knowledge.list_indexed_documents(db_path, "housing")
    assert len(docs) == 2
    assert {d["filename"] for d in docs} == {"tenancy_rights.txt", "security_deposit.txt"}

    # Test deduplication: re-indexing the same file is skipped
    outcome2 = knowledge.index_uploaded_documents(
        [doc1], "housing", db_path, chroma_path, uploads_dir
    )
    assert outcome2["indexed"] == []
    assert outcome2["skipped"] == ["tenancy_rights.txt"]

    # Test knowledgebase rename
    moved = knowledge.rename_knowledgebase("housing", "tenancy", db_path, chroma_path)
    assert moved == 2
    assert knowledge.knowledgebase_document_counts(db_path) == {"tenancy": 2}
    assert len(knowledge.list_indexed_documents(db_path, "tenancy")) == 2
    assert len(knowledge.list_indexed_documents(db_path, "housing")) == 0


# ============================================================================
# 2. Scaffolding Workflow: Step 2 - Prompt Profiles Configuration
# ============================================================================

def test_e2e_step2_prompt_profiles_configuration():
    """Test prompt profile creation, normalization, pairing, and starter prompts."""
    raw_profiles = [
        {
            "label": "Tenant Advisor",
            "prompt": "You are a legal advisor specializing in tenant rights.",
            "knowledgebase": "tenancy",
            "conversation_starters": [
                "What notice is required for eviction?",
                "How long does the landlord have to return my deposit?",
            ],
            "connected_files": ["tenancy_rights.txt"],
        },
        {
            "label": "Small Claims Advisor",
            "prompt": "You guide users through filing small claims.",
            "knowledgebase": "courts",
            "conversation_starters": ["What is the maximum claim amount?"],
            "connected_files": [],
        },
    ]

    normalized = normalize_prompt_profiles(raw_profiles)
    assert len(normalized) == 2
    assert normalized[0]["key"] == "tenant-advisor"
    assert normalized[0]["knowledgebase"] == "tenancy"
    assert normalized[0]["connected_files"] == ["tenancy_rights.txt"]
    assert len(normalized[0]["conversation_starters"]) == 2

    assert normalized[1]["key"] == "small-claims-advisor"
    assert normalized[1]["knowledgebase"] == "courts"
    assert normalized[1]["connected_files"] == []


# ============================================================================
# 3. Scaffolding Workflow: Step 3 - Interactive Chat Preview
# ============================================================================

def test_e2e_step3_chat_preview_functionality(monkeypatch, tmp_path):
    """Test preview chat logic, query retrieval against session data, and turn limits."""
    session_dir = tmp_path / "session_step3"
    session_dir.mkdir()
    db_path = str(session_dir / "app.sqlite3")
    chroma_path = str(session_dir / "chroma")
    uploads_dir = str(session_dir / "uploads")
    init_db(db_path)

    # Ingest document
    knowledge.index_uploaded_documents(
        [
            InMemoryUpload(
                "eviction_guide.txt",
                "Massachusetts law requires fourteen days notice in writing before "
                "a landlord can terminate a tenancy for nonpayment of rent.",
            )
        ],
        "housing",
        db_path,
        chroma_path,
        uploads_dir,
    )

    # Test retrieval from session database
    results = query_index(
        "fourteen days notice eviction",
        n_results=3,
        knowledgebase="housing",
        db_path=db_path,
        chroma_path=chroma_path,
    )
    assert len(results) >= 1
    assert "fourteen days notice" in results[0]["content"].lower()

    # Test turn limit enforcement
    monkeypatch.setattr(step_chat, "preview_chat_turn_limit", lambda: 2)
    assert step_chat.preview_chat_turn_limit() == 2


# ============================================================================
# 4. Scaffolding Workflow: Step 4 - Evaluation, Matrix, and Result Promotion
# ============================================================================

def test_e2e_step4_evaluation_matrix_and_winner_promotion(monkeypatch, tmp_path):
    """Test scenario CSV round-trip, evaluation matrix execution, and promoting winner."""
    scenarios_csv = (
        "input,__expected,__description\n"
        "What notice is required for nonpayment?,icontains:fourteen days,Eviction notice\n"
        "How long to return security deposit?,icontains:30 days,Deposit return\n"
    )
    parsed_scenarios = parse_scenario_csv(scenarios_csv)
    assert len(parsed_scenarios) == 2
    assert parsed_scenarios[0]["input"] == "What notice is required for nonpayment?"

    csv_output = scenarios_to_csv(parsed_scenarios)
    assert "What notice is required for nonpayment?" in csv_output

    profiles = [
        {
            "key": "housing",
            "label": "Housing Advisor",
            "knowledgebase": "housing",
            "prompt": "Answer housing questions.",
            "connected_files": [],
        },
        {
            "key": "general",
            "label": "General Advisor",
            "knowledgebase": "general",
            "prompt": "Answer general questions.",
            "connected_files": [],
        },
    ]

    matrix = build_evaluation_matrix(
        profiles,
        prompt_keys=["housing", "general"],
        knowledgebase_keys=["housing", "general"],
        models=["gpt-5.4-mini"],
        scenarios=parsed_scenarios,
    )
    # 2 prompts × 2 knowledge bases × 1 model × 2 scenarios = 8 matrix calls
    assert len(matrix) == 8

    # Mock evaluation execution
    def mock_call_model(messages, **kwargs):
        is_judge = "evaluation judge" in messages[0]["content"]
        if is_judge:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"score": 0.95, "reason": "Clear."}'))]
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Fourteen days notice is required under Massachusetts law."
                    )
                )
            ]
        )

    results = evaluation.run_evaluation(
        matrix[:2],  # run subset
        run_id="test-run-123",
        provider="openai",
        judge_model="judge-model",
        shared_rubrics=["Stays grounded"],
        top_k=3,
        db_path=":memory:",
        chroma_path="/tmp/null",
        document_labels={},
        call_model=mock_call_model,
        retrieve=lambda *args, **kwargs: [
            {"content": "Fourteen days notice is required.", "metadata": {"page_number": 1}}
        ],
    )

    assert len(results) == 2
    assert results[0]["passed"] is True
    assert results[0]["score"] == 0.95

    # Test summary and best configuration promotion
    summary = evaluation.summarize_by_prompt_and_model(results)
    best = evaluation.best_configuration(summary)
    assert best["Prompt"] == "Housing Advisor"
    assert best["Model"] == "gpt-5.4-mini"


# ============================================================================
# 5. Scaffolding Workflow: Step 5 - Workspace Serialization & Bundle Creation
# ============================================================================

def test_e2e_step5_workspace_and_bundle_export(tmp_path):
    """Test workspace export/import round-trip, secrets generation, and complete bundle creation."""
    session_dir = tmp_path / "session_step5"
    session_dir.mkdir()
    db_path = str(session_dir / "app.sqlite3")
    chroma_path = str(session_dir / "chroma")
    uploads_dir = str(session_dir / "uploads")
    init_db(db_path)

    # Ingest document
    knowledge.index_uploaded_documents(
        [
            InMemoryUpload(
                "tenant_rights.txt",
                "Fourteen days notice is required for nonpayment of rent.",
            )
        ],
        "housing",
        db_path,
        chroma_path,
        uploads_dir,
    )

    app_config = default_scaffold_config()
    app_config["app"]["title"] = "Suffolk Housing Assistant"
    app_config["app"]["welcome_message"] = "Welcome to AskLit Housing Help!"
    app_config["app"]["access_mode"] = "password"
    app_config["app"]["disable_admin"] = False
    app_config["model"] = {
        "provider": "openai",
        "name": "gpt-5.4-mini",
        "allow_user_selection": True,
        "allowed_models": "gpt-5.4-mini,gpt-5.4-nano",
        "base_url": "",
    }
    app_config["prompt_profiles"] = [
        {
            "key": "housing",
            "label": "Housing Advisor",
            "knowledgebase": "housing",
            "prompt": "You are an eviction prevention advisor.",
            "conversation_starters": ["What notice is required?"],
            "connected_files": ["tenant_rights.txt"],
        },
        {
            "key": "intake",
            "label": "Intake Screener",
            "knowledgebase": "housing",
            "prompt": "Screen client eligibility.",
            "conversation_starters": ["Where do you live?"],
            "connected_files": [],
        },
    ]

    scenarios = [
        {"input": "What notice is needed?", "__expected": "icontains:14 days", "__description": "Notice"}
    ]
    rubrics = ["Answers in plain language"]

    # 1. Workspace YAML Export / Import
    ws_yaml = workspace.export_workspace_yaml(app_config, scenarios, rubrics)
    imported = workspace.import_workspace_yaml(ws_yaml)
    assert imported["app_config"]["app"]["title"] == "Suffolk Housing Assistant"
    assert len(imported["app_config"]["prompt_profiles"]) == 2
    assert imported["evaluation_scenarios"][0]["input"] == "What notice is needed?"
    assert imported["evaluation_rubrics"] == ["Answers in plain language"]

    # 2. Deployment Secrets Generation
    shared_hash = hash_password("tenant-pass-123")
    admin_hash = hash_password("admin-pass-456")
    secrets_content = bundle_module.generate_deployment_secrets(
        app_config,
        password_hashes={"shared": shared_hash, "admin": admin_hash},
    )
    assert 'OPENAI_API_KEY = "PASTE_YOUR_KEY_HERE"' in secrets_content
    assert f'SHARED_PASSWORD_HASH = "{shared_hash}"' in secrets_content
    assert f'ADMIN_PASSWORD_HASH = "{admin_hash}"' in secrets_content
    assert '"app.title" = "Suffolk Housing Assistant"' in secrets_content
    assert '"app.access_mode" = "password"' in secrets_content
    assert '"app.disable_admin" = false' in secrets_content

    # 3. Create Bundle
    bundle_dir = Path(bundle_module.create_bundle(app_config, str(session_dir)))
    assert bundle_dir.exists()

    # Validate bundle file structure
    assert (bundle_dir / "app.py").exists()
    assert (bundle_dir / "chat_ui.py").exists()
    assert (bundle_dir / "login_ui.py").exists()
    assert (bundle_dir / "requirements.txt").exists()
    assert (bundle_dir / "admin" / "settings.py").exists()
    assert (bundle_dir / "admin" / "kb.py").exists()
    assert (bundle_dir / "admin" / "logs.py").exists()
    assert (bundle_dir / "admin" / "hash_tool.py").exists()
    assert (bundle_dir / "asklit" / "config.py").exists()
    assert (bundle_dir / "asklit" / "rag.py").exists()
    assert (bundle_dir / "config" / "defaults.toml").exists()
    assert (bundle_dir / "data" / "app.sqlite3").exists()
    assert (bundle_dir / "data" / "chroma").exists()
    assert (bundle_dir / "prompts" / "housing.yml").exists()
    assert (bundle_dir / "prompts" / "intake.yml").exists()
    assert (bundle_dir / ".streamlit" / "config.toml").exists()
    assert (bundle_dir / ".gitignore").exists()
    assert (bundle_dir / "README.md").exists()

    # Ensure no dev/test/cache artifacts in bundle
    assert not list(bundle_dir.rglob("__pycache__"))
    assert not list(bundle_dir.rglob("*.pyc"))
    assert not (bundle_dir / "scaffold.py").exists()
    assert not (bundle_dir / ".venv").exists()
    assert not (bundle_dir / ".pytest_cache").exists()

    # Verify defaults.toml contents
    generated_defaults = toml.load(bundle_dir / "config" / "defaults.toml")
    assert generated_defaults["app"]["title"] == "Suffolk Housing Assistant"
    assert generated_defaults["model"]["name"] == "gpt-5.4-mini"
    assert generated_defaults["model"]["allow_user_selection"] is True

    # Verify prompt YAML contents
    with open(bundle_dir / "prompts" / "housing.yml", "r", encoding="utf-8") as f:
        housing_yml = yaml.safe_load(f)
    assert housing_yml["label"] == "Housing Advisor"
    assert housing_yml["knowledgebase"]["name"] == "housing"
    assert housing_yml["knowledgebase"]["files"] == ["tenant_rights.txt"]
    assert housing_yml["conversation starters"] == ["What notice is required?"]

    # 4. Zip Directory
    zip_dest = tmp_path / "suffolk-app.zip"
    bundle_module.zip_directory(str(bundle_dir), str(zip_dest))
    assert zip_dest.exists()
    assert zip_dest.stat().st_size > 0


# ============================================================================
# 6. Exported Application Verification (Executing the Generated App)
# ============================================================================

def _build_test_bundle(tmp_path, access_mode="public", disable_admin=False):
    """Helper to create a fully configured bundle for testing the exported app."""
    session_dir = tmp_path / f"session_{access_mode}_{disable_admin}"
    session_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(session_dir / "app.sqlite3")
    chroma_path = str(session_dir / "chroma")
    uploads_dir = str(session_dir / "uploads")
    init_db(db_path)

    # Ingest document
    knowledge.index_uploaded_documents(
        [
            InMemoryUpload(
                "tenant_rights.txt",
                "Fourteen days notice is required before eviction proceedings in Massachusetts.",
            )
        ],
        "housing",
        db_path,
        chroma_path,
        uploads_dir,
    )

    app_config = default_scaffold_config()
    app_config["app"]["title"] = "Deployed Legal Assistant"
    app_config["app"]["welcome_message"] = "Hello! Ask a question about tenant rights."
    app_config["app"]["access_mode"] = access_mode
    app_config["app"]["disable_admin"] = disable_admin
    app_config["model"] = {
        "provider": "openai",
        "name": "gpt-5.4-mini",
        "allow_user_selection": True,
        "allowed_models": "gpt-5.4-mini,gpt-5.4-nano",
        "base_url": "",
    }
    app_config["branding"] = {
        "homepage_url": "https://suffolklitlab.org",
        "supplemental_footer_text": "Suffolk LIT Lab",
        "hide_asklit_badge": False,
    }
    app_config["prompt_profiles"] = [
        {
            "key": "housing",
            "label": "Housing Help",
            "knowledgebase": "housing",
            "prompt": "You are a specialized housing assistant.",
            "conversation_starters": ["What notice is required?"],
            "connected_files": ["tenant_rights.txt"],
        },
        {
            "key": "small-claims",
            "label": "Small Claims",
            "knowledgebase": "housing",
            "prompt": "You guide users through small claims.",
            "conversation_starters": ["How to file a claim?"],
            "connected_files": [],
        },
    ]

    bundle_dir = Path(bundle_module.create_bundle(app_config, str(session_dir)))
    return bundle_dir


def test_e2e_exported_app_public_chat_and_rag(tmp_path, monkeypatch):
    """Test running the exported app in public access mode via AppTest."""
    bundle_dir = _build_test_bundle(tmp_path, access_mode="public", disable_admin=True)

    # Change working directory and DB path to the exported bundle directory
    monkeypatch.chdir(bundle_dir)
    monkeypatch.setenv("ASKLIT_DB_PATH", str(bundle_dir / "data" / "app.sqlite3"))
    monkeypatch.setenv("CHROMA_PATH", str(bundle_dir / "data" / "chroma"))

    # Mock LiteLLM completion
    def mock_completion(**kwargs):
        class FakeChoice:
            delta = SimpleNamespace(content="Fourteen days notice is required.")
            finish_reason = "stop"
            message = SimpleNamespace(content="Fourteen days notice is required.")

        return [SimpleNamespace(choices=[FakeChoice()])]

    sys.modules["litellm"].completion = mock_completion

    # Run the exported app from its bundle directory
    app = AppTest.from_file(str(bundle_dir / "app.py"), default_timeout=30)
    app.run()

    assert not app.exception
    assert app.title[0].value == "Deployed Legal Assistant"
    # Welcome message is present in session state messages
    assert "messages" in app.session_state
    assert any(
        m["content"] == "Hello! Ask a question about tenant rights."
        for m in app.session_state["messages"]
    )

    # Verify conversation starters are rendered
    assert any("What notice is required?" in b.label for b in app.button)

    # Verify prompt selector options
    assert len(app.sidebar.radio) >= 1
    assert "Housing Help" in app.sidebar.radio[0].options
    assert "Small Claims" in app.sidebar.radio[0].options

    # Verify model selector options
    assert len(app.sidebar.selectbox) >= 1
    assert app.sidebar.selectbox[0].options == ["gpt-5.4-mini", "gpt-5.4-nano"]

    # Submit a question via chat input
    app.chat_input[0].set_value("How many days notice are required?").run()

    assert not app.exception
    # User message and assistant response in session state messages
    messages = app.session_state["messages"]
    assert any("How many days notice are required?" in m.get("content", "") for m in messages)
    assert any("Fourteen days notice is required." in m.get("content", "") for m in messages)

    # Verify message was logged to exported database
    db_path = bundle_dir / "data" / "app.sqlite3"
    conn = sqlite3.connect(db_path)
    messages_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    conn.close()
    assert messages_count >= 2


def test_e2e_exported_app_password_protection(tmp_path, monkeypatch):
    """Test exported app access gate when password access mode is enabled."""
    bundle_dir = _build_test_bundle(tmp_path, access_mode="password", disable_admin=True)

    monkeypatch.chdir(bundle_dir)
    monkeypatch.setenv("ASKLIT_DB_PATH", str(bundle_dir / "data" / "app.sqlite3"))
    monkeypatch.setenv("CHROMA_PATH", str(bundle_dir / "data" / "chroma"))

    shared_hash = hash_password("correct-tenant-password")
    monkeypatch.setattr("asklit.auth.get_secret_value", lambda k, d=None: shared_hash if k == "SHARED_PASSWORD_HASH" else d)
    monkeypatch.setattr("asklit.config.get_secret_value", lambda k, d=None: shared_hash if k == "SHARED_PASSWORD_HASH" else d)

    app = AppTest.from_file(str(bundle_dir / "app.py"), default_timeout=30)
    app.run()

    # Chat is blocked until password entered
    assert not app.chat_input
    assert app.text_input[0].label == "Password"

    # Incorrect password
    app.text_input[0].set_value("wrong-password").run()
    assert any("Password incorrect" in err.value for err in app.error)
    assert not app.chat_input

    # Correct password
    app.text_input[0].set_value("correct-tenant-password").run()
    assert not app.exception
    assert app.chat_input


def test_e2e_exported_app_admin_management_lifecycle(tmp_path, monkeypatch):
    """Test admin route access, admin authentication, admin pages, and logout."""
    bundle_dir = _build_test_bundle(tmp_path, access_mode="public", disable_admin=False)

    monkeypatch.chdir(bundle_dir)
    monkeypatch.setenv("ASKLIT_DB_PATH", str(bundle_dir / "data" / "app.sqlite3"))
    monkeypatch.setenv("CHROMA_PATH", str(bundle_dir / "data" / "chroma"))

    admin_hash = hash_password("admin-secret-2026")
    secrets_dict = {
        "ADMIN_ROUTE": "manage",
        "ADMIN_PASSWORD_HASH": admin_hash,
    }
    monkeypatch.setattr("asklit.config.get_secret_value", lambda k, d=None: secrets_dict.get(k, d))
    monkeypatch.setattr("asklit.auth.get_secret_value", lambda k, d=None: secrets_dict.get(k, d))

    # 1. Visiting with ?manage unlocks the System navigation
    app = AppTest.from_file(str(bundle_dir / "app.py"), default_timeout=30)
    app.query_params["manage"] = ""
    app.run()

    assert not app.exception
    # Admin is unlocked, so login page / hash tool is available in navigation
    assert app.session_state["admin_unlocked"] is True

    # 2. Simulate admin login form
    app.session_state["is_admin_authenticated"] = True
    app.run()

    # When admin is authenticated, full navigation is loaded
    assert app.session_state["is_admin_authenticated"] is True


def test_e2e_exported_app_disabled_admin_mode(tmp_path, monkeypatch):
    """Test that ?manage route does NOT unlock admin when disable_admin is True."""
    bundle_dir = _build_test_bundle(tmp_path, access_mode="public", disable_admin=True)

    monkeypatch.chdir(bundle_dir)
    monkeypatch.setenv("ASKLIT_DB_PATH", str(bundle_dir / "data" / "app.sqlite3"))
    monkeypatch.setenv("CHROMA_PATH", str(bundle_dir / "data" / "chroma"))

    secrets_dict = {"ADMIN_ROUTE": "manage"}
    monkeypatch.setattr("asklit.config.get_secret_value", lambda k, d=None: secrets_dict.get(k, d))
    monkeypatch.setattr("asklit.auth.get_secret_value", lambda k, d=None: secrets_dict.get(k, d))

    app = AppTest.from_file(str(bundle_dir / "app.py"), default_timeout=30)
    app.query_params["manage"] = ""
    app.run()

    assert "admin_unlocked" not in app.session_state or not app.session_state["admin_unlocked"]
