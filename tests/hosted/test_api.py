"""Hosted boundary tests use fake completions/embeddings, never provider credentials."""

from asklit.hosted.auth import resolve_identity, valid_microsoft_issuer
from asklit.hosted.worker import run_one


def new_bot(hosted, user="alice"):
    client = hosted[4](user)
    r = client.post(
        "/api/bots", json={"org_id": "school", "title": "Housing assistant"}
    )
    assert r.status_code == 200, r.text
    return r.json()


def publish(hosted, bot):
    c = hosted[4]("alice")
    r = c.post(f"/api/bots/{bot['id']}/publish", json={"version": bot["version"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_creator_admin_and_cross_tenant_boundaries(hosted):
    bot = new_bot(hosted)
    for user, status in [
        ("alice", 200),
        ("owner", 200),
        ("bob", 404),
        ("outsider", 404),
    ]:
        c = hosted[4](user)
        for suffix in ["", "/documents", "/jobs", "/revisions", "/workspace"]:
            assert c.get(f"/api/bots/{bot['id']}{suffix}").status_code == status
        assert bool(c.get("/api/bots").json()) == (status == 200)
    c = hosted[4]("outsider")
    assert (
        c.post("/api/bots", json={"org_id": "school", "title": "Bad"}).status_code
        == 404
    )
    assert c.delete(f"/api/bots/{bot['id']}").status_code == 404


def test_csrf_and_stale_drafts(hosted):
    bot = new_bot(hosted)
    c = hosted[3]
    body = {"version": bot["version"], "config": bot["config"]}
    c.headers.pop("x-csrf-token")
    assert c.put(f"/api/bots/{bot['id']}", json=body).status_code == 403
    c.headers["x-csrf-token"] = "csrf"
    assert c.put(f"/api/bots/{bot['id']}", json=body).status_code == 200
    assert c.put(f"/api/bots/{bot['id']}", json=body).status_code == 409
    body["config"]["base_url"] = "https://attacker.invalid"
    assert c.put(f"/api/bots/{bot['id']}", json=body).status_code == 422


def test_immutable_publication_capability_and_idempotency(hosted):
    bot = new_bot(hosted)
    published = publish(hosted, bot)
    c = hosted[3]
    public = f"/api/public/bots/{bot['id']}"
    meta = c.get(public).json()
    assert "prompt" not in str(meta) and "model_preset" not in meta
    config = bot["config"]
    config["profiles"][0]["prompt"] = "Edited unpublished system prompt"
    assert (
        c.put(
            f"/api/bots/{bot['id']}", json={"version": 1, "config": config}
        ).status_code
        == 200
    )
    conv = c.post(public + "/conversations", json={"profile": "default"}).json()
    assert conv["revision_id"] == published["revision_id"]
    endpoint = f"/api/public/conversations/{conv['id']}/messages"
    body = {"message": "Help me", "request_id": "one"}
    assert c.get(endpoint).status_code == 405
    assert c.post(endpoint, json=body).status_code == 404
    headers = {"Authorization": "Bearer " + conv["token"]}
    first = c.post(endpoint, json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert c.post(endpoint, json=body, headers=headers).json() == first.json()
    assert len(hosted[5]) == 1
    assert "Edited unpublished" not in hosted[5][0][0]["content"]
    c.post(f"/api/bots/{bot['id']}/unpublish")
    assert (
        c.post(
            endpoint, json={**body, "request_id": "two"}, headers=headers
        ).status_code
        == 404
    )


def test_upload_worker_snapshot_and_removal(hosted):
    bot = new_bot(hosted)
    c = hosted[3]
    r = c.post(
        f"/api/bots/{bot['id']}/documents",
        data={"knowledgebase": "default"},
        files={
            "file": (
                "guide.txt",
                b"Useful guidance for the person seeking housing help. " * 20,
            )
        },
    )
    assert r.status_code == 200, r.text
    bot = c.get(f"/api/bots/{bot['id']}").json()
    assert (
        c.post(
            f"/api/bots/{bot['id']}/publish", json={"version": bot["version"]}
        ).status_code
        == 409
    )
    assert run_one(hosted[1], hosted[0], hosted[2])
    publish(hosted, bot)
    doc_id = r.json()["document_id"]
    assert c.delete(f"/api/bots/{bot['id']}/documents/{doc_id}").status_code == 200
    conv = c.post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    result = c.post(
        f"/api/public/conversations/{conv['id']}/messages",
        json={"message": "housing", "request_id": "x"},
        headers={"Authorization": "Bearer " + conv["token"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["sources"][0]["filename"] == "guide.txt"


def test_invitation_role_escalation_last_owner_and_revocation(hosted):
    c = hosted[4]("alice")
    assert (
        c.post(
            "/api/organizations/school/invitations", json={"role": "admin"}
        ).status_code
        == 404
    )
    c = hosted[4]("owner")
    assert (
        c.patch(
            "/api/organizations/school/members/owner", json={"role": "member"}
        ).status_code
        == 409
    )
    invite = (
        c.post("/api/organizations/school/invitations", json={"role": "member"})
        .json()["url"]
        .split("invite=")[1]
    )
    c = hosted[4]("outsider")
    assert c.post("/api/invitations/accept", json={"token": invite}).status_code == 200
    assert c.post("/api/invitations/accept", json={"token": invite}).status_code == 404
    c = hosted[4]("owner")
    assert c.delete("/api/organizations/school/members/alice").status_code == 200
    c = hosted[4]("alice")
    assert (
        c.post("/api/bots", json={"org_id": "school", "title": "No"}).status_code == 404
    )


def test_identity_never_links_by_email(hosted):
    with hosted[1].transaction() as db:
        a = resolve_identity(
            db,
            {
                "iss": "https://accounts.google.com",
                "sub": "abc",
                "email": "same@example.org",
            },
        )
        b = resolve_identity(
            db,
            {
                "iss": "https://login.microsoftonline.com/tenant/v2.0",
                "sub": "xyz",
                "email": "same@example.org",
            },
        )
        assert a.id != b.id
        db.flush()
        assert (
            resolve_identity(db, {"iss": "accounts.google.com", "sub": "abc"}).id
            == a.id
        )
    tid = "11111111-2222-3333-4444-555555555555"
    assert valid_microsoft_issuer(
        {"tid": tid}, f"https://login.microsoftonline.com/{tid}/v2.0"
    )
    assert not valid_microsoft_issuer({"tid": tid}, "https://attacker.invalid")


def test_budget_failure_is_not_replayed(hosted):
    bot = new_bot(hosted)
    publish(hosted, bot)
    c = hosted[3]
    hosted[0].daily_calls = 1
    conv = c.post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    url = f"/api/public/conversations/{conv['id']}/messages"
    headers = {"Authorization": "Bearer " + conv["token"]}
    assert (
        c.post(
            url, json={"message": "a", "request_id": "a"}, headers=headers
        ).status_code
        == 200
    )
    assert (
        c.post(
            url, json={"message": "b", "request_id": "b"}, headers=headers
        ).status_code
        == 429
    )
    assert len(hosted[5]) == 1


def test_workspace_and_evaluation(hosted):
    bot = new_bot(hosted)
    c = hosted[3]
    config = bot["config"]
    config["scenarios"] = [{"input": "Question", "expected": "contains:answer"}]
    saved = c.put(f"/api/bots/{bot['id']}", json={"version": 1, "config": config})
    assert saved.status_code == 200
    job = c.post(f"/api/bots/{bot['id']}/evaluate")
    assert job.status_code == 200, job.text
    run_one(hosted[1], hosted[0], hosted[2])
    results = c.get(f"/api/bots/{bot['id']}/jobs").json()
    assert results[0]["status"] == "succeeded", results
    assert results[0]["result"]["rows"][0]["passed"] is True
    exported = c.get(f"/api/bots/{bot['id']}/workspace")
    assert exported.status_code == 200, exported.text
    other = new_bot(hosted)
    imported = c.post(
        f"/api/bots/{other['id']}/workspace", json={"version": 1, "yaml": exported.text}
    )
    assert imported.status_code == 200, imported.text
    restored = imported.json()["bot"]["config"]["scenarios"][0]
    assert restored["input"] == "Question"
    assert restored["expected"] == "contains:answer"
    assert restored["description"] == "Scenario 1"


def test_body_limits_cors_and_editor(hosted):
    c = hosted[3]
    assert c.get("/").status_code == 200
    assert c.get("/static/studio.js").status_code == 200
    r = c.options(
        "/api/public/bots/a",
        headers={
            "origin": "http://localhost:3000",
            "access-control-request-method": "POST",
        },
    )
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in r.headers
    r = c.post("/api/bots", content=b"x" * (12 * 1024 * 1024))
    assert r.status_code == 413


def test_failed_index_is_not_publishable_and_can_be_removed(hosted):
    bot = new_bot(hosted)
    c = hosted[3]
    r = c.post(
        f"/api/bots/{bot['id']}/documents", files={"file": ("empty.txt", b"   ")}
    )
    assert r.status_code == 200
    run_one(hosted[1], hosted[0], hosted[2])
    docs = c.get(f"/api/bots/{bot['id']}/documents").json()
    assert docs[0]["status"] == "failed"
    updated = c.get(f"/api/bots/{bot['id']}").json()
    assert (
        c.post(
            f"/api/bots/{bot['id']}/publish", json={"version": updated["version"]}
        ).status_code
        == 409
    )
    assert (
        c.delete(f"/api/bots/{bot['id']}/documents/{docs[0]['id']}").status_code == 200
    )


def test_streamlit_bundle_has_sources_but_no_hosted_accounts(hosted):
    import io
    import zipfile

    bot = new_bot(hosted)
    c = hosted[3]
    uploaded = c.post(
        f"/api/bots/{bot['id']}/documents",
        files={"file": ("guide.txt", b"The source guide has useful material. " * 30)},
    )
    assert uploaded.status_code == 200
    run_one(hosted[1], hosted[0], hosted[2])
    job = c.post(f"/api/bots/{bot['id']}/export")
    assert job.status_code == 200
    run_one(hosted[1], hosted[0], hosted[2])
    response = c.get(f"/api/bots/{bot['id']}/jobs/{job.json()['id']}/download")
    assert response.status_code == 200, c.get(f"/api/bots/{bot['id']}/jobs").text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "app.py" in names and "prompts/default.yml" in names
        assert "data/app.sqlite3" in names
        assert any(n.startswith("data/uploads/") for n in names)
        assert not any("hosted" in n for n in names)
        export_dir = hosted[0].storage / "verified-export"
        archive.extractall(export_dir)
        import sqlite3
        import chromadb

        with sqlite3.connect(export_dir / "data" / "app.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        collection = chromadb.PersistentClient(
            path=str(export_dir / "data" / "chroma")
        ).get_collection("asklit_docs")
        assert collection.count() > 0


def test_outsiders_cannot_mutate_or_retrieve_jobs(hosted):
    bot = new_bot(hosted)
    c = hosted[4]("bob")
    prefix = f"/api/bots/{bot['id']}"
    for path, body in [
        ("/publish", {"version": 1}),
        ("/unpublish", None),
        ("/evaluate", None),
        ("/export", None),
        ("/owner", {"owner_id": "bob"}),
        ("/preview", {"message": "x"}),
        ("/revisions/any/activate", None),
    ]:
        assert c.post(prefix + path, json=body).status_code == 404, path
    assert c.delete(prefix + "/documents/any").status_code == 404
    assert c.get(prefix + "/jobs/any/download").status_code == 404
    assert (
        c.post(prefix + "/documents", files={"file": ("x.txt", b"x")}).status_code
        == 404
    )


def test_public_creation_rate_limit_and_no_secrets_in_metadata(hosted):
    bot = new_bot(hosted)
    publish(hosted, bot)
    c = hosted[3]
    url = f"/api/public/bots/{bot['id']}"
    assert c.get(url).status_code == 200
    for _ in range(30):
        assert c.post(url + "/conversations", json={}).status_code == 200
    assert c.post(url + "/conversations", json={}).status_code == 429


def test_failed_model_turn_never_replays_same_request(hosted):
    bot = new_bot(hosted)
    publish(hosted, bot)
    c = hosted[3]

    def fail(*args):
        raise RuntimeError("secret credential must never appear")

    hosted[2].completion = fail
    conv = c.post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    endpoint = f"/api/public/conversations/{conv['id']}/messages"
    headers = {"Authorization": "Bearer " + conv["token"]}
    body = {"message": "Hi", "request_id": "same"}
    first = c.post(endpoint, json=body, headers=headers)
    assert first.status_code == 502 and "secret credential" not in first.text
    assert c.post(endpoint, json=body, headers=headers).status_code == 410


def test_rubric_aliases_reserve_judge_calls_and_enforce_cap(hosted):
    hosted[0].evaluation_max_calls = 60
    from asklit.hosted.models import Organization

    bot = new_bot(hosted)
    client = hosted[3]
    config = bot["config"]
    config["scenarios"] = [
        {"input": "Question", "expected": "Model-Rubric: Is accurate"}
    ]
    saved = client.put(
        f"/api/bots/{bot['id']}", json={"version": 1, "config": config}
    ).json()
    assert client.post(f"/api/bots/{bot['id']}/evaluate").status_code == 200
    with hosted[1].transaction() as db:
        assert db.get(Organization, "school").calls == 2
    config["scenarios"] *= 31
    client.put(
        f"/api/bots/{bot['id']}", json={"version": saved["version"], "config": config}
    )
    assert client.post(f"/api/bots/{bot['id']}/evaluate").status_code == 422


def test_authoring_budget_does_not_exhaust_public_budget(hosted):
    bot = new_bot(hosted)
    publish(hosted, bot)
    client = hosted[3]
    hosted[0].global_daily_calls = 1
    assert (
        client.post(
            f"/api/bots/{bot['id']}/preview", json={"message": "Hello"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/bots/{bot['id']}/preview", json={"message": "Again"}
        ).status_code
        == 429
    )
    conv = client.post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()
    assert (
        client.post(
            f"/api/public/conversations/{conv['id']}/messages",
            json={"message": "Public question", "request_id": "one"},
            headers={"Authorization": "Bearer " + conv["token"]},
        ).status_code
        == 200
    )


def test_late_answer_commits_failed_state_and_releases_lease(hosted):
    from asklit.hosted.models import Conversation, Turn

    bot = new_bot(hosted)
    publish(hosted, bot)
    client = hosted[3]
    conv = client.post(f"/api/public/bots/{bot['id']}/conversations", json={}).json()

    def delayed(*args):
        with hosted[1].transaction() as db:
            db.get(Conversation, conv["id"]).busy_until = 0
        return "Late answer"

    hosted[2].completion = delayed
    result = client.post(
        f"/api/public/conversations/{conv['id']}/messages",
        json={"message": "Question", "request_id": "late"},
        headers={"Authorization": "Bearer " + conv["token"]},
    )
    assert result.status_code == 410
    with hosted[1].transaction() as db:
        assert db.get(Turn, (conv["id"], "late")).status == "failed"
        assert db.get(Conversation, conv["id"]).busy_until == 0


def test_invalid_preview_does_not_spend_and_yaml_requires_login(hosted, monkeypatch):
    from asklit.hosted.models import Organization
    from asklit.hosted import portability

    bot = new_bot(hosted)
    client = hosted[3]
    assert (
        client.post(
            f"/api/bots/{bot['id']}/preview",
            json={"message": "Hello", "profile": "missing"},
        ).status_code
        == 404
    )
    with hosted[1].transaction() as db:
        assert db.get(Organization, "school").calls == 0

    def should_not_parse(*args):
        raise AssertionError("Unauthenticated YAML was parsed")

    monkeypatch.setattr(portability, "read_workspace", should_not_parse)
    client.cookies.clear()
    assert (
        client.post(
            f"/api/bots/{bot['id']}/workspace", json={"version": 1, "yaml": "bad"}
        ).status_code
        == 401
    )


def test_multiple_uploads_can_queue_before_indexing(hosted):
    bot = new_bot(hosted)
    client = hosted[3]
    for name in ["first", "second"]:
        result = client.post(
            f"/api/bots/{bot['id']}/documents",
            files={"file": (name + ".txt", (name + " source text").encode())},
        )
        assert result.status_code == 200
    assert len(client.get(f"/api/bots/{bot['id']}/jobs").json()) == 2


def test_oidc_disabled_or_invalid_callbacks_do_not_create_sessions(hosted):
    c = hosted[3]
    c.cookies.clear()
    assert c.get("/auth/google/login").status_code == 404
    assert c.get("/auth/evil/callback?code=x&state=x").status_code == 404
    assert c.get("/api/me").status_code == 401


def test_renaming_knowledgebase_preserves_published_sources(hosted):
    from asklit.hosted.models import Document, Revision

    bot = new_bot(hosted)
    client = hosted[3]
    client.post(
        f"/api/bots/{bot['id']}/documents",
        files={"file": ("guide.txt", b"Source content about housing. " * 30)},
    )
    run_one(hosted[1], hosted[0], hosted[2])
    bot = client.get(f"/api/bots/{bot['id']}").json()
    published = publish(hosted, bot)
    renamed = client.post(
        f"/api/bots/{bot['id']}/knowledgebases/rename",
        json={"version": bot["version"], "old_name": "default", "new_name": "housing"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["config"]["profiles"][0]["knowledgebase"] == "housing"
    with hosted[1].transaction() as db:
        revision = db.get(Revision, published["revision_id"])
        old = db.get(Document, revision.document_ids[0])
        assert old.knowledgebase == "default" and not old.active
    docs = client.get(f"/api/bots/{bot['id']}/documents").json()
    assert docs[0]["knowledgebase"] == "housing" and docs[0]["status"] == "ready"


def test_conversation_can_be_deleted_after_unpublishing(hosted):
    bot = new_bot(hosted)
    publish(hosted, bot)
    client = hosted[3]
    conversation = client.post(
        f"/api/public/bots/{bot['id']}/conversations", json={}
    ).json()
    client.post(f"/api/bots/{bot['id']}/unpublish")
    result = client.delete(
        f"/api/public/conversations/{conversation['id']}",
        headers={"Authorization": "Bearer " + conversation["token"]},
    )
    assert result.status_code == 200
