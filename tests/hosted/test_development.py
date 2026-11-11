"""Local development uses the same sessions, permissions and migrated schema."""

import re

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from asklit.hosted.app import create_app
from asklit.hosted.models import Store
from asklit.hosted.settings import Settings


def csrf(client):
    page = client.get("/")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_sqlite_migrations_and_local_login(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASKLIT_DEV_MODE", "true")
    monkeypatch.setenv(
        "ASKLIT_DATABASE_URL", f"sqlite:///{tmp_path}/data/hosted.sqlite3"
    )
    monkeypatch.setenv("ASKLIT_DEV_USERNAME", "tester@example.test")
    monkeypatch.setenv("ASKLIT_DEV_PASSWORD", "fixed-local-password")
    monkeypatch.delenv("ASKLIT_SECRET_KEY", raising=False)
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "asklit/hosted/migrations"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    settings = Settings.from_env()
    store = Store(settings.database_url)
    assert "email_verified" in {
        c["name"] for c in inspect(store.engine).get_columns("hosted_identities")
    }
    with TestClient(create_app(settings, store)) as client:
        assert client.post("/auth/local/login", data={}).status_code == 403
        response = client.post(
            "/auth/local/login",
            data={
                "csrf": csrf(client),
                "username": settings.dev_username,
                "password": "wrong",
            },
        )
        assert "Incorrect username or password." in response.text
        assert client.get("/api/me").status_code == 401
        credentials = {
            "csrf": csrf(client),
            "username": settings.dev_username,
            "password": settings.dev_password,
        }
        assert client.post("/auth/local/login", data=credentials).status_code == 200
        me = client.get("/api/me").json()
        assert me["email"] == settings.dev_username
        assert me["organizations"][0]["role"] == "owner"
        client.headers["x-csrf-token"] = me["csrf"]
        bot = client.post(
            "/api/bots",
            json={"org_id": me["organizations"][0]["id"], "title": "Local test"},
        )
        assert bot.status_code == 200
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/me").status_code == 401
        credentials["csrf"] = csrf(client)
        assert client.post("/auth/local/login", data=credentials).status_code == 200
        assert client.get("/api/me").json()["id"] == me["id"]
        assert len(client.get("/api/bots").json()) == 1
    store.engine.dispose()


def test_local_login_absent_without_dev_mode(hosted):
    client = hosted[3]
    assert "Sign in locally" not in client.get("/").text
    assert client.post(
        "/auth/local/login", data={"username": "dev@asklit.local", "password": "asklit"}
    ).status_code in {404, 405}


def test_sqlite_requires_explicit_development_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASKLIT_DEV_MODE", "false")
    monkeypatch.setenv("ASKLIT_DATABASE_URL", "sqlite:///local.sqlite3")
    monkeypatch.setenv("ASKLIT_SECRET_KEY", "x" * 40)
    with pytest.raises(RuntimeError, match="enable ASKLIT_DEV_MODE"):
        Settings.from_env()


def test_dev_mode_rejects_public_origin():
    with pytest.raises(ValueError, match="loopback"):
        Settings(
            database_url="sqlite:///local.sqlite3",
            secret_key="local",
            dev_mode=True,
            base_url="https://studio.example.org",
        )
