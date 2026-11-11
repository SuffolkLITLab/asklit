"""Operator-owned settings. Browser input never selects credentials or endpoints."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID


@dataclass
class Settings:
    database_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    speech_base_url: str = "https://api.openai.com/v1"
    speech_api_key: str = field(default="", repr=False)
    speech_key_header: str = "Authorization"
    speech_stt_model: str = ""
    speech_tts_model: str = ""
    speech_voice: str = "alloy"
    speech_daily_calls: int = 1000
    trust_fly_proxy: bool = False
    dev_mode: bool = False
    dev_username: str = "dev@asklit.local"
    dev_password: str = field(default="asklit", repr=False)
    base_url: str = "http://localhost:8000"
    storage: Path = Path("hosted-data")
    origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])
    models: dict = field(default_factory=dict)
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    microsoft_client_id: str = ""
    microsoft_client_secret: str = field(default="", repr=False)
    # Tenant-independent Entra discovery supports school/work accounts.
    microsoft_tenant: str = "organizations"
    embedding_model: str = "all-MiniLM-L6-v2"
    evaluation_max_calls: int = 240
    daily_calls: int = 500
    global_daily_calls: int = 5000
    public_daily_calls: int = 5000
    max_upload_bytes: int = 10 * 1024 * 1024
    retention_days: int = 7
    job_timeout_seconds: int = 1800
    storage_backend: str = "filesystem"
    supabase_url: str = ""
    supabase_bucket: str = "asklit-private"
    supabase_service_role_key: str = field(default="", repr=False)

    def __post_init__(self):
        speech_url = urlparse(self.speech_base_url)
        if (not speech_url.hostname or speech_url.username or speech_url.password
            or speech_url.query or speech_url.fragment
            or (speech_url.scheme != "https" and not (
                speech_url.scheme == "http" and speech_url.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("Speech endpoint must use HTTPS, or HTTP on loopback, without credentials or a query.")
        if self.speech_key_header not in {"Authorization", "api-key", "Ocp-Apim-Subscription-Key"}:
            raise ValueError("Unsupported speech API key header.")
        if self.speech_daily_calls < 1:
            raise ValueError("Speech allowance must be positive.")
        if self.dev_mode:
            if urlparse(self.base_url).hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("ASKLIT_DEV_MODE requires a loopback ASKLIT_BASE_URL.")
            if not self.dev_username or not self.dev_password:
                raise ValueError("Local development credentials cannot be empty.")
        if self.storage_backend not in {"filesystem", "supabase"}:
            raise ValueError("ASKLIT_STORAGE_BACKEND must be filesystem or supabase.")
        if self.storage_backend == "supabase" and (
            not self.supabase_url or not self.supabase_service_role_key
        ):
            raise ValueError(
                "Supabase storage requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
            )
        if self.storage_backend == "supabase":
            endpoint = urlparse(self.supabase_url)
            local_http = endpoint.scheme == "http" and endpoint.hostname in {
                "localhost",
                "127.0.0.1",
                "::1",
            }
            if (
                not endpoint.hostname
                or (endpoint.scheme != "https" and not local_http)
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
            ):
                raise ValueError(
                    "SUPABASE_URL must use HTTPS (HTTP is allowed only on loopback)."
                )
        if self.microsoft_tenant != "organizations":
            try:
                valid_tenant = (
                    str(UUID(self.microsoft_tenant)) == self.microsoft_tenant.lower()
                )
            except ValueError:
                valid_tenant = False
            if not valid_tenant:
                raise ValueError(
                    "MICROSOFT_TENANT must be 'organizations' or a tenant UUID."
                )
        if (
            min(
                self.evaluation_max_calls,
                self.daily_calls,
                self.global_daily_calls,
                self.public_daily_calls,
                self.retention_days,
                self.job_timeout_seconds,
            )
            < 1
        ):
            raise ValueError(
                "Hosted quotas, retention, and job timeouts must be positive."
            )
        for preset in self.models.values():
            if not isinstance(preset, dict) or not isinstance(preset.get("model"), str):
                raise ValueError("Each hosted model preset must name a model.")
            if preset.get("base_url"):
                endpoint = urlparse(preset["base_url"])
                if (
                    endpoint.scheme not in {"http", "https"}
                    or not endpoint.netloc
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                ):
                    raise ValueError(
                        "Model endpoints must be HTTP(S) URLs without embedded credentials, query strings, or fragments."
                    )

    @property
    def secure(self):
        return self.base_url.startswith("https://")

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv

        load_dotenv(".env.hosted", override=False)
        dev_mode = os.getenv("ASKLIT_DEV_MODE", "false").lower() in {"1", "true", "yes"}
        secret = os.environ.get(
            "ASKLIT_SECRET_KEY",
            "asklit-local-development-secret-only" if dev_mode else "",
        )
        if len(secret) < 32 or secret.startswith("replace-"):
            raise RuntimeError(
                "Set ASKLIT_SECRET_KEY to a random secret of at least 32 characters."
            )
        database = os.environ.get(
            "ASKLIT_DATABASE_URL",
            "sqlite:///hosted-data/hosted.sqlite3" if dev_mode else "",
        )
        if not (
            database.startswith("postgresql+psycopg://")
            or (dev_mode and database.startswith("sqlite:///"))
        ):
            raise RuntimeError(
                "Set ASKLIT_DATABASE_URL to a postgresql+psycopg:// URL, or enable ASKLIT_DEV_MODE for SQLite."
            )
        models = json.loads(os.environ.get("ASKLIT_MODELS_JSON", "{}"))
        if not models:
            models = {
                "default": {
                    "provider": "openai",
                    "model": "gpt-4.1-nano" if dev_mode else "gpt-5.4-mini",
                    "api_key_env": "OPENAI_API_KEY",
                }
            }
        return cls(
            database_url=database,
            speech_base_url=os.getenv("ASKLIT_SPEECH_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            speech_api_key=os.getenv("ASKLIT_SPEECH_API_KEY", ""),
            speech_key_header=os.getenv("ASKLIT_SPEECH_KEY_HEADER", "Authorization"),
            speech_stt_model=os.getenv("ASKLIT_SPEECH_STT_MODEL", ""),
            speech_tts_model=os.getenv("ASKLIT_SPEECH_TTS_MODEL", ""),
            speech_voice=os.getenv("ASKLIT_SPEECH_VOICE", "alloy"),
            speech_daily_calls=int(os.getenv("ASKLIT_SPEECH_DAILY_CALLS", "1000")),
            dev_mode=dev_mode,
            trust_fly_proxy=os.getenv("ASKLIT_TRUST_FLY_PROXY", "false").lower() == "true",
            dev_username=os.getenv("ASKLIT_DEV_USERNAME", "dev@asklit.local"),
            dev_password=os.getenv("ASKLIT_DEV_PASSWORD", "asklit"),
            secret_key=secret,
            base_url=os.getenv("ASKLIT_BASE_URL", "http://localhost:8000").rstrip("/"),
            storage=Path(os.getenv("ASKLIT_STORAGE", "hosted-data")).resolve(),
            origins=[
                s.strip()
                for s in os.getenv(
                    "ASKLIT_CORS_ORIGINS", "http://localhost:3000"
                ).split(",")
                if s.strip()
            ],
            models=models,
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            microsoft_client_id=os.getenv("MICROSOFT_CLIENT_ID", ""),
            microsoft_client_secret=os.getenv("MICROSOFT_CLIENT_SECRET", ""),
            microsoft_tenant=os.getenv("MICROSOFT_TENANT", "organizations"),
            embedding_model=os.getenv("ASKLIT_EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
            evaluation_max_calls=int(os.getenv("ASKLIT_EVALUATION_MAX_CALLS", "240")),
            daily_calls=int(os.getenv("ASKLIT_DAILY_CALLS", "500")),
            global_daily_calls=int(os.getenv("ASKLIT_GLOBAL_DAILY_CALLS", "5000")),
            public_daily_calls=int(os.getenv("ASKLIT_PUBLIC_DAILY_CALLS", "5000")),
            retention_days=int(os.getenv("ASKLIT_RETENTION_DAYS", "7")),
            job_timeout_seconds=int(os.getenv("ASKLIT_JOB_TIMEOUT_SECONDS", "1800")),
            storage_backend=os.getenv("ASKLIT_STORAGE_BACKEND", "filesystem"),
            supabase_url=os.getenv("SUPABASE_URL", "").rstrip("/"),
            supabase_bucket=os.getenv("SUPABASE_STORAGE_BUCKET", "asklit-private"),
            supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        )
