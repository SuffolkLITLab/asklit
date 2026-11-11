"""Exercise actual Authlib state, PKCE and signed-token validation without live SSO."""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from authlib.integrations.starlette_client import StarletteOAuth2App
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from asklit.hosted.app import create_app


@pytest.mark.parametrize(
    "invalid", [None, "aud", "iss", "nonce", "exp", "signature", "state"]
)
@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_signed_callback(hosted, monkeypatch, invalid, provider):
    settings, store, runtime = hosted[:3]
    settings.google_client_id = "test-client"
    settings.google_client_secret = "test-secret"
    settings.microsoft_client_id = "test-client"
    settings.microsoft_client_secret = "test-secret"
    tenant = "11111111-2222-3333-4444-555555555555"
    issuer = (
        "https://accounts.google.com"
        if provider == "google"
        else f"https://login.microsoftonline.com/{tenant}/v2.0"
    )
    key = RSAKey.generate_key()
    public_key = key.as_dict(private=False)
    public_key["kid"] = "test-key"
    expected = {}

    async def metadata(self):
        return {
            "issuer": issuer
            if provider == "google"
            else "https://login.microsoftonline.com/{tenantid}/v2.0",
            "authorization_endpoint": "https://accounts.google.com/authorize",
            "token_endpoint": "https://accounts.google.com/token",
            "jwks_uri": "https://accounts.google.com/keys",
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    async def jwks(self, **kwargs):
        return {"keys": [public_key]}

    async def access_token(self, **kwargs):
        assert kwargs.get("code_verifier"), (
            "PKCE verifier must be sent during code exchange"
        )
        claims = {
            "iss": issuer,
            "tid": tenant,
            "sub": "oidc-user",
            "aud": "test-client",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
            "nonce": expected["nonce"],
            "email": "oidc@example.invalid",
            "name": "OIDC reviewer",
        }
        if invalid in {"aud", "iss", "nonce"}:
            claims[invalid] = "attacker-value"
        if invalid == "exp":
            claims["exp"] = int(time.time()) - 1000
        signing_key = RSAKey.generate_key() if invalid == "signature" else key
        return {
            "id_token": jwt.encode(
                {"alg": "RS256", "kid": "test-key"}, claims, signing_key
            )
        }

    monkeypatch.setattr(StarletteOAuth2App, "load_server_metadata", metadata)
    monkeypatch.setattr(StarletteOAuth2App, "fetch_jwk_set", jwks)
    monkeypatch.setattr(StarletteOAuth2App, "fetch_access_token", access_token)
    with TestClient(create_app(settings, store, runtime)) as client:
        redirect = client.get(f"/auth/{provider}/login", follow_redirects=False)
        assert redirect.status_code == 302
        query = parse_qs(urlparse(redirect.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        expected["nonce"] = query["nonce"][0]
        state = "wrong-state" if invalid == "state" else query["state"][0]
        result = client.get(
            f"/auth/{provider}/callback",
            params={"code": "fake-code", "state": state},
            follow_redirects=False,
        )
        if invalid:
            assert result.status_code == 400, result.text
            assert client.get("/api/me").status_code == 401
        else:
            assert result.status_code == 303, result.text
            me = client.get("/api/me")
            assert me.status_code == 200
            assert me.json()["name"] == "OIDC reviewer"
            assert me.json()["organizations"][0]["role"] == "owner"
            # OAuth state cannot be replayed to mint another application session.
            assert (
                client.get(
                    f"/auth/{provider}/callback",
                    params={"code": "fake-code", "state": state},
                ).status_code
                == 400
            )
