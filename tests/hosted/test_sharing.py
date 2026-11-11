import pytest
from asklit.hosted.models import Identity, PasswordKey, LoginSession
from asklit.hosted.auth import resolve_identity
from asklit.hosted.security import digest
from test_api import new_bot, publish


def org_settings(hosted, **changes):
    c = hosted[4]("owner")
    body = {
        "slug": "school",
        "gallery_enabled": True,
        "allowed_domains": [],
        "microsoft_tenants": [],
        **changes,
    }
    r = c.put("/api/organizations/school/sharing", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def share(hosted, bot, mode="public", slug="housing"):
    r = hosted[4]("alice").put(
        f"/api/bots/{bot['id']}/sharing", json={"slug": slug, "access_mode": mode}
    )
    assert r.status_code == 200, r.text
    return r.json()


def setup_bot(hosted, mode="public"):
    org_settings(hosted)
    bot = new_bot(hosted)
    share(hosted, bot, mode)
    publish(hosted, bot)
    return bot


def test_gallery_and_stable_aliases_use_published_snapshot(hosted):
    bot = setup_bot(hosted)
    c = hosted[3]
    base = f"/api/bots/{bot['id']}"
    bot["config"]["title"] = "Private draft name"
    bot["config"]["description"] = "Secret draft description"
    assert (
        c.put(
            base, json={"version": bot["version"], "config": bot["config"]}
        ).status_code
        == 200
    )
    gallery = c.get("/api/public/gallery").json()
    assert len(gallery["items"]) == 1
    assert gallery["items"][0]["title"] == "Housing assistant"
    assert "Secret draft" not in str(gallery)
    assert c.get("/o/school/housing").status_code == 200
    share(hosted, bot, slug="new-housing")
    org_settings(hosted, slug="new-school")
    old = c.get("/o/school/housing", follow_redirects=False)
    assert (
        old.status_code == 308
        and old.headers["location"] == "/new-school/new-housing"
    )
    assert (
        c.get("/api/public/organizations/school/bots/housing").json()["id"] == bot["id"]
    )
    other = new_bot(hosted)
    assert (
        c.put(
            f"/api/bots/{other['id']}/sharing",
            json={"slug": "housing", "access_mode": "public"},
        ).status_code
        == 409
    )
    share(hosted, bot, "unlisted", "new-housing")
    assert c.get("/api/public/gallery").json()["items"] == []
    assert c.get("/o/new-school/new-housing").status_code == 200
    c.post(base + "/unpublish")
    assert c.get("/o/school/housing").status_code == 404


@pytest.mark.parametrize(
    "slug",
    [
        "../escape",
        "two/segments",
        "bad_slug",
        "<script>",
        "%2fsecret",
        "spaced name",
        "-leading",
        "trailing-",
        "double--dash",
        "https://evil.test",
        "ümlaut",
    ],
)
def test_slug_validation(hosted, slug):
    bot = new_bot(hosted)
    assert (
        hosted[3]
        .put(
            f"/api/bots/{bot['id']}/sharing",
            json={"slug": slug, "access_mode": "public"},
        )
        .status_code
        == 422
    )
    assert (
        hosted[4]("owner")
        .put("/api/organizations/school/sharing", json={"slug": slug})
        .status_code
        == 422
    )


def test_link_management_is_tenant_scoped(hosted):
    bot = setup_bot(hosted)
    c = hosted[4]("bob")
    assert (
        c.put(
            f"/api/bots/{bot['id']}/sharing",
            json={"slug": "stolen", "access_mode": "public"},
        ).status_code
        == 404
    )
    assert (
        c.put("/api/organizations/school/sharing", json={"slug": "stolen"}).status_code
        == 404
    )
    c = hosted[4]("outsider")
    assert (
        c.put("/api/organizations/other/sharing", json={"slug": "school"}).status_code
        == 409
    )
    other = c.post(
        "/api/bots", json={"org_id": "other", "title": "Other organization"}
    ).json()
    assert (
        c.put(
            f"/api/bots/{other['id']}/sharing",
            json={"slug": "housing", "access_mode": "public"},
        ).status_code
        == 200
    )


def password_setup(hosted):
    bot = setup_bot(hosted, "password")
    c = hosted[3]
    key = c.post(
        f"/api/bots/{bot['id']}/passwords",
        json={"label": "Class code", "password": "1234"},
    ).json()
    return bot, key


def unlock(hosted, bot, password="1234"):
    c = hosted[3]
    r = c.post(f"/api/public/bots/{bot['id']}/access", json={"password": password})
    assert r.status_code == 200, r.text
    grant = r.json()
    conversation = c.post(
        f"/api/public/bots/{bot['id']}/conversations",
        json={"access_token": grant["token"]},
    ).json()
    return grant, conversation


def test_passwords_are_independent_revocable_and_never_exposed(hosted):
    bot, key = password_setup(hosted)
    c = hosted[3]
    metadata = c.get(f"/api/public/bots/{bot['id']}").json()
    assert (
        metadata["locked"]
        and metadata["profiles"] == []
        and metadata["title"] == "Protected AskLit"
    )
    assert (
        c.post(f"/api/public/bots/{bot['id']}/conversations", json={}).status_code
        == 403
    )
    assert (
        c.post(
            f"/api/public/bots/{bot['id']}/access", json={"password": "wrong"}
        ).status_code
        == 403
    )
    grant, conversation = unlock(hosted, bot)
    assert grant["metadata"]["title"] == "Housing assistant"
    token = {"Authorization": "Bearer " + conversation["token"]}
    path = f"/api/public/conversations/{conversation['id']}/messages"
    assert (
        c.post(
            path, headers=token, json={"message": "Hi", "request_id": "one"}
        ).status_code
        == 200
    )
    c.patch(f"/api/bots/{bot['id']}/passwords/{key['id']}", json={"enabled": False})
    assert (
        c.post(
            path, headers=token, json={"message": "Hi", "request_id": "one"}
        ).status_code
        == 403
    )  # cached replies too
    assert (
        c.post(
            f"/api/public/bots/{bot['id']}/access", json={"password": "1234"}
        ).status_code
        == 403
    )
    c.patch(f"/api/bots/{bot['id']}/passwords/{key['id']}", json={"enabled": True})
    assert (
        c.post(
            path, headers=token, json={"message": "Hi", "request_id": "one"}
        ).status_code
        == 403
    )  # old access stays revoked
    unlock(hosted, bot)
    other = c.post(
        f"/api/bots/{bot['id']}/passwords",
        json={"label": "Second group", "password": "5678"},
    )
    assert other.status_code == 200
    assert len(c.get(f"/api/bots/{bot['id']}/passwords").json()) == 2
    with hosted[1].transaction() as db:
        stored = db.get(PasswordKey, key["id"])
        assert "1234" not in stored.password_hash and stored.password_hash != "1234"
    assert "password_hash" not in c.get(f"/api/bots/{bot['id']}/passwords").text
    assert c.get("/api/public/gallery").json()["items"] == []


def test_access_change_during_completion_discards_response(hosted):
    bot, key = password_setup(hosted)
    _, conversation = unlock(hosted, bot)

    def completion(messages, preset):
        with hosted[1].transaction() as db:
            db.get(PasswordKey, key["id"]).enabled = False
        return "Must not be returned"

    hosted[2].completion = completion
    r = hosted[3].post(
        f"/api/public/conversations/{conversation['id']}/messages",
        headers={"Authorization": "Bearer " + conversation["token"]},
        json={"message": "Hi", "request_id": "one"},
    )
    assert r.status_code == 404 and "Must not be returned" not in r.text


def test_wrong_password_attempts_commit_rate_limit(hosted):
    bot, key = password_setup(hosted)
    for _ in range(60):
        assert (
            hosted[3]
            .post(f"/api/public/bots/{bot['id']}/access", json={"password": "wrong"})
            .status_code
            == 403
        )
    assert (
        hosted[3]
        .post(f"/api/public/bots/{bot['id']}/access", json={"password": "1234"})
        .status_code
        == 429
    )


def test_organization_access_requires_authoritative_identity_and_revokes(hosted):
    bot = setup_bot(hosted, "organization")
    org_settings(hosted, allowed_domains=["school.edu", "alumni.school.edu"])
    c = hosted[4]("outsider")
    url = f"/api/public/bots/{bot['id']}/access"
    with hosted[1].transaction() as db:
        db.add(
            Identity(
                issuer="https://accounts.google.com",
                subject="outsider",
                user_id="outsider",
                email_verified=True,
                hosted_domain="",
            )
        )
    assert (
        c.post(url, json={}).status_code == 403
    )  # email_verified alone is insufficient
    with hosted[1].transaction() as db:
        db.get(
            Identity, ("https://accounts.google.com", "outsider")
        ).hosted_domain = "school.edu"
    r = c.post(url, json={})
    assert r.status_code == 200, r.text
    grant = r.json()["token"]
    conversation = c.post(
        f"/api/public/bots/{bot['id']}/conversations", json={"access_token": grant}
    ).json()
    assert (
        c.get("/api/public/gallery?organization=school").json()["items"][0]["id"]
        == bot["id"]
    )
    org_settings(hosted, allowed_domains=["different.edu"])
    assert (
        c.post(
            f"/api/public/conversations/{conversation['id']}/messages",
            headers={"Authorization": "Bearer " + conversation["token"]},
            json={"message": "Hi", "request_id": "one"},
        ).status_code
        == 403
    )
    with hosted[1].transaction() as db:
        resolve_identity(
            db,
            {
                "iss": "https://accounts.google.com",
                "sub": "outsider",
                "email": "changed@example.test",
                "email_verified": True,
                "hd": "different.edu",
            },
        )
    hosted[4]("outsider")
    new = c.post(url, json={})
    assert new.status_code == 200
    with hosted[1].transaction() as db:
        db.delete(db.get(LoginSession, digest("outsider")))
    assert (
        c.post(
            f"/api/public/bots/{bot['id']}/conversations",
            json={"access_token": new.json()["token"]},
        ).status_code
        == 403
    )


def test_microsoft_tenant_rules_and_no_email_suffix_trust(hosted):
    bot = setup_bot(hosted, "organization")
    tenant = "11111111-1111-4111-8111-111111111111"
    org_settings(hosted, microsoft_tenants=[tenant])
    with hosted[1].transaction() as db:
        db.add(
            Identity(
                issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
                subject="outsider",
                user_id="outsider",
                email_verified=False,
            )
        )
    c = hosted[4]("outsider")
    assert c.post(f"/api/public/bots/{bot['id']}/access", json={}).status_code == 200
    org_settings(hosted, microsoft_tenants=[])
    hosted[4]("outsider")
    assert c.post(f"/api/public/bots/{bot['id']}/access", json={}).status_code == 403


def test_reserved_generated_slugs(hosted):
    bot = new_bot(hosted)
    assert hosted[3].put(
        f"/api/bots/{bot['id']}/sharing",
        json={"slug": "asklit-" + "f" * 32, "access_mode": "public"},
    ).status_code == 422
    assert hosted[4]("owner").put(
        "/api/organizations/school/sharing",
        json={"slug": "org-" + "f" * 32},
    ).status_code == 422


def test_expired_unlock_keeps_existing_conversation_but_not_new_ones(hosted):
    from asklit.hosted.models import AccessGrant, Bot, Conversation
    from asklit.hosted.access import conversation_allowed
    from sqlalchemy import select
    bot, _ = password_setup(hosted)
    grant, conversation = unlock(hosted, bot)
    with hosted[1].transaction() as db:
        access = db.scalar(select(AccessGrant).where(AccessGrant.bot_id == bot["id"]))
        access.expires = 1
        db.flush()
        assert conversation_allowed(db, db.get(Bot, bot["id"]), db.get(Conversation, conversation["id"]), hosted[0])
    assert hosted[3].post(
        f"/api/public/bots/{bot['id']}/conversations",
        json={"access_token": grant["token"]},
    ).status_code == 403


def test_author_edits_only_suffix_and_cannot_change_organization(hosted):
    bot = setup_bot(hosted)
    response = hosted[3].put(
        f"/api/bots/{bot['id']}/sharing",
        json={"slug": "new-guide", "access_mode": "public"},
    )
    assert response.json()["url"].endswith("/school/new-guide")
    assert hosted[3].get("/school/new-guide").status_code == 200
    old = hosted[3].get("/o/school/new-guide", follow_redirects=False)
    assert old.status_code == 308
    assert old.headers["location"] == "/school/new-guide"
    assert hosted[3].put(
        "/api/organizations/school/sharing", json={"slug": "other"}
    ).status_code == 404
    assert hosted[3].put(
        f"/api/bots/{bot['id']}/sharing",
        json={"slug": "school/new-guide", "access_mode": "public"},
    ).status_code == 422


def test_default_slug_uses_title_and_preserves_links(hosted):
    c = hosted[4]("alice")
    def create(title):
        response = c.post("/api/bots", json={"org_id": "school", "title": title})
        assert response.status_code == 200, response.text
        bot = response.json()
        sharing = c.get(f"/api/bots/{bot['id']}/sharing").json()
        return bot, sharing["slug"]

    bot, slug = create("Café Housing / Help?!")
    assert slug == "cafe-housing-help"
    assert create("Café Housing / Help?!")[1] == "cafe-housing-help-2"
    bot["config"]["title"] = "Changed display name"
    assert c.put(f"/api/bots/{bot['id']}", json={"version": bot["version"], "config": bot["config"]}).status_code == 200
    assert c.get(f"/api/bots/{bot['id']}/sharing").json()["slug"] == slug
    assert c.delete(f"/api/bots/{bot['id']}").status_code == 200
    assert create("Café Housing / Help?!")[1] == "cafe-housing-help-3"
    assert create("你好 !!!")[1] == "asklit"
    assert create("A")[1] == "asklit-a"
    assert len(create("a" * 200)[1]) == 80
    assert create("a" * 200)[1] == "a" * 78 + "-2"
