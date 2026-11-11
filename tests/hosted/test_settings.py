import pytest

from asklit.hosted.settings import Settings


@pytest.mark.parametrize(
    "url",
    [
        "http://project.supabase.co",
        "ftp://localhost",
        "https://user:password@project.supabase.co",
        "https://project.supabase.co?key=secret",
    ],
)
def test_reject_insecure_storage_endpoint(url):
    with pytest.raises(ValueError, match="SUPABASE_URL"):
        Settings(
            database_url="unused",
            secret_key="secret",
            storage_backend="supabase",
            supabase_url=url,
            supabase_service_role_key="secret",
        )


def test_settings_repr_does_not_expose_credentials():
    settings = Settings(
        database_url="database-password",
        secret_key="session-secret",
        google_client_secret="google-secret",
        microsoft_client_secret="microsoft-secret",
        supabase_service_role_key="storage-secret",
    )
    for value in [
        "database-password",
        "session-secret",
        "google-secret",
        "microsoft-secret",
        "storage-secret",
    ]:
        assert value not in repr(settings)
