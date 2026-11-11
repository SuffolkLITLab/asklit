"""Hosted boundary tests use fake completions/embeddings, never provider credentials."""

import time
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from asklit.hosted.app import create_app
from asklit.hosted.models import LoginSession, Membership, Organization, Store, User
from asklit.hosted.runtime import Runtime
from asklit.hosted.security import digest
from asklit.hosted.settings import Settings


@pytest.fixture
def hosted(tmp_path):
    pg_url = os.getenv("ASKLIT_TEST_DATABASE_URL")
    schema = "test_" + uuid.uuid4().hex
    admin = None
    url = f"sqlite:///{tmp_path}/hosted.sqlite3"
    if pg_url:
        admin = Store(pg_url)
        with admin.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = (
            make_url(pg_url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
    settings = Settings(
        database_url=url,
        secret_key="x" * 40,
        storage=tmp_path / "objects",
        models={"default": {"model": "fake", "provider": "openai"}},
    )
    store = Store(settings.database_url)
    store.initialize()
    calls = []

    def completion(messages, preset):
        calls.append(messages)
        return "Grounded answer"

    runtime = Runtime(
        settings,
        embedder=lambda texts, model: [[1.0, 0.0] for _ in texts],
        completion=completion,
    )
    app = create_app(settings, store, runtime)
    with store.transaction() as db:
        for user_id in ["owner", "alice", "bob", "outsider"]:
            db.add(User(id=user_id, email=f"{user_id}@example.org", name=user_id))
        db.add(Organization(id="school", name="School"))
        db.add(Organization(id="other", name="Other"))
        db.flush()
        for user_id, role in [
            ("owner", "owner"),
            ("alice", "member"),
            ("bob", "member"),
        ]:
            db.add(Membership(org_id="school", user_id=user_id, role=role))
        db.add(Membership(org_id="other", user_id="outsider", role="owner"))
        for user_id in ["owner", "alice", "bob", "outsider"]:
            db.add(
                LoginSession(
                    token_hash=digest(user_id),
                    user_id=user_id,
                    csrf="csrf",
                    expires=time.time() + 3600,
                )
            )
    client = TestClient(app)

    def as_user(user):
        client.cookies.set("asklit_session", user)
        client.headers["x-csrf-token"] = "csrf"
        return client

    yield settings, store, runtime, client, as_user, calls
    client.close()
    store.engine.dispose()
    if admin:
        with admin.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.engine.dispose()
