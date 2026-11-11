"""Opt-in checks against a disposable local Supabase stack, never a cloud project."""

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from dotenv import dotenv_values
from sqlalchemy import text

from asklit.hosted.models import Store
from asklit.hosted.settings import Settings
from asklit.hosted.storage import object_store

pytestmark = pytest.mark.skipif(
    not os.getenv("ASKLIT_SUPABASE_TEST_ENV"),
    reason="Requires a disposable local Supabase stack",
)


@pytest.fixture
def supabase():
    values = dotenv_values(os.environ["ASKLIT_SUPABASE_TEST_ENV"])
    assert urlparse(values["API_URL"]).hostname in {"127.0.0.1", "localhost"}
    return values


def authenticated_token(values):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(
        b"="
    )
    data = (
        encode({"alg": "HS256", "typ": "JWT"})
        + b"."
        + encode(
            {
                "sub": str(uuid4()),
                "role": "authenticated",
                "aud": "authenticated",
                "exp": int(time.time()) + 300,
            }
        )
    )
    signature = base64.urlsafe_b64encode(
        hmac.new(values["JWT_SECRET"].encode(), data, hashlib.sha256).digest()
    ).rstrip(b"=")
    return (data + b"." + signature).decode()


def test_supabase_data_api_cannot_bypass_backend_authorization(supabase):
    for bearer in [supabase["ANON_KEY"], authenticated_token(supabase)]:
        for table in [
            "hosted_users",
            "hosted_bots",
            "hosted_login_sessions",
            "hosted_documents",
            "hosted_conversations",
        ]:
            response = httpx.get(
                f"{supabase['API_URL']}/rest/v1/{table}?select=*",
                headers={
                    "apikey": supabase["ANON_KEY"],
                    "Authorization": "Bearer " + bearer,
                },
            )
            assert response.status_code in {401, 403, 404}, (
                table,
                response.status_code,
            )


def test_supabase_rls_is_enabled_on_every_hosted_table(supabase):
    store = Store(
        supabase["DB_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
    )
    with store.transaction() as db:
        rows = db.execute(
            text(
                "SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public' AND (tablename LIKE 'hosted\\_%' ESCAPE '\\' OR tablename='asklit_schema_version')"
            )
        ).all()
        assert len(rows) >= 15
        assert all(enabled for _, enabled in rows), rows


def test_supabase_private_storage_round_trip_and_no_browser_access(supabase):
    settings = Settings(
        database_url="unused",
        secret_key="local-test-only-secret",
        storage_backend="supabase",
        supabase_url=supabase["API_URL"],
        supabase_service_role_key=supabase["SERVICE_ROLE_KEY"],
    )
    storage = object_store(settings)
    key = uuid4().hex + ".txt"
    data = b"Synthetic private source for the Supabase integration test."
    try:
        storage.put_bytes(key, data)
        with storage.materialize(key) as path:
            assert path.read_bytes() == data
        public = httpx.get(
            f"{supabase['API_URL']}/storage/v1/object/public/asklit-private/{key}"
        )
        assert public.status_code in {400, 401, 403, 404}
        authenticated = httpx.get(
            f"{supabase['API_URL']}/storage/v1/object/authenticated/asklit-private/{key}",
            headers={
                "apikey": supabase["ANON_KEY"],
                "Authorization": "Bearer " + authenticated_token(supabase),
            },
        )
        assert authenticated.status_code in {400, 401, 403, 404}
    finally:
        httpx.request(
            "DELETE",
            f"{supabase['API_URL']}/storage/v1/object/asklit-private",
            json={"prefixes": [key]},
            headers={
                "apikey": supabase["SERVICE_ROLE_KEY"],
                "Authorization": "Bearer " + supabase["SERVICE_ROLE_KEY"],
            },
        )


def test_migration_privacy_guard_covers_future_tables(supabase):
    from asklit.hosted.migrations.privacy import PRIVACY_SQL

    store = Store(
        supabase["DB_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
    )
    with store.engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text("CREATE TABLE hosted_future_guard_test (secret text)")
            )
            connection.execute(
                text("GRANT SELECT ON hosted_future_guard_test TO anon, authenticated")
            )
            connection.execute(text(PRIVACY_SQL))
            assert connection.execute(
                text(
                    "SELECT rowsecurity FROM pg_tables WHERE schemaname='public' AND tablename='hosted_future_guard_test'"
                )
            ).scalar_one()
            for role in ["anon", "authenticated"]:
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege(:role, 'hosted_future_guard_test', 'SELECT')"
                    ),
                    {"role": role},
                ).scalar_one()
        finally:
            transaction.rollback()
    store.engine.dispose()
