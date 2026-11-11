"""Opt-in LOCAL Supabase fixture server/worker for tiny-model browser tests.

This does not add a login bypass to the application. Bootstrap creates a test
user/session directly in a disposable local database. Real OIDC callbacks are
covered separately by signed-token tests. Never point this script at production.
"""

import argparse
import json
import os
import secrets
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["bootstrap", "api", "worker"])
    parser.add_argument("--supabase-env", default="/tmp/asklit-supabase.env")
    parser.add_argument(
        "--database-url-file", default="/tmp/asklit-supabase-pooler-url"
    )
    parser.add_argument("--model-secrets", default=".streamlit/secrets.toml")
    parser.add_argument("--session-file", default="/tmp/asklit-live-session.json")
    args = parser.parse_args()
    supabase = dotenv_values(args.supabase_env)
    database = (
        Path(args.database_url_file).read_text().strip()
        if Path(args.database_url_file).is_file()
        else supabase["DB_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
    )
    if urlparse(database).hostname not in {"localhost", "127.0.0.1"} or urlparse(
        supabase["API_URL"]
    ).hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit(
            "This fixture only supports disposable LOCAL Supabase instances."
        )
    model_secrets = tomllib.loads(Path(args.model_secrets).read_text())
    os.environ.update(
        {
            "ASKLIT_DATABASE_URL": database,
            "ASKLIT_SECRET_KEY": "local-e2e-only-not-a-production-cookie-secret",
            "ASKLIT_BASE_URL": "http://127.0.0.1:8049",
            "ASKLIT_STORAGE_BACKEND": "supabase",
            "SUPABASE_URL": supabase["API_URL"],
            "SUPABASE_SERVICE_ROLE_KEY": supabase["SERVICE_ROLE_KEY"],
            "SUPABASE_STORAGE_BUCKET": "asklit-private",
            "ASKLIT_E2E_MODEL_KEY": model_secrets["OPENAI_API_KEY"],
            "ASKLIT_MODELS_JSON": json.dumps(
                {
                    "tiny": {
                        "provider": "openai",
                        "model": "gpt-4.1-nano",
                        "base_url": model_secrets.get("OPENAI_BASE_URL", ""),
                        "api_key_env": "ASKLIT_E2E_MODEL_KEY",
                        "max_tokens": 128,
                    }
                }
            ),
        }
    )
    from asklit.hosted.settings import Settings

    settings = Settings.from_env()
    if args.role == "bootstrap":
        from asklit.hosted.models import (
            Store,
            User,
            Organization,
            Membership,
            LoginSession,
        )
        from asklit.hosted.security import digest

        store = Store(settings.database_url)
        raw = secrets.token_urlsafe(32)
        with store.transaction() as db:
            user = User(
                email="synthetic-reviewer@example.invalid", name="Supabase E2E Reviewer"
            )
            org = Organization(name="Supabase E2E School")
            db.add_all([user, org])
            db.flush()
            db.add(Membership(org_id=org.id, user_id=user.id, role="owner"))
            db.add(
                LoginSession(
                    token_hash=digest(raw),
                    user_id=user.id,
                    csrf=secrets.token_urlsafe(32),
                    expires=time.time() + 7200,
                )
            )
        path = Path(args.session_file)
        path.write_text(
            json.dumps({"token": raw, "org_id": org.id, "user_id": user.id})
        )
        path.chmod(0o600)
        print("Local synthetic account/session created; no credentials printed.")
    elif args.role == "api":
        import uvicorn

        uvicorn.run(
            "asklit.hosted.app:create_app",
            factory=True,
            host="127.0.0.1",
            port=8049,
            access_log=False,
        )
    else:
        from asklit.hosted.worker import main as worker

        sys.argv = [sys.argv[0]]
        worker()


if __name__ == "__main__":
    main()
