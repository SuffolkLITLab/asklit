import copy
import json

from asklit.hosted.models import Job, Organization
from asklit.hosted.worker import run_one
from test_api import new_bot


def matrix_bot(hosted):
    settings, store, runtime, client = hosted[:4]
    settings.models.update(
        {"second": {"model": "fake-second"}, "judge": {"model": "fake-judge"}}
    )
    bot = new_bot(hosted)
    config = bot["config"]
    config["profiles"].append(
        {
            **copy.deepcopy(config["profiles"][0]),
            "key": "other",
            "label": "Other",
            "knowledgebase": "other",
            "prompt": "Alternate instructions",
        }
    )
    config["scenarios"] = [
        {
            "input": f"Question {i}",
            "expected": "icontains:answer",
            "description": f"Case {i}",
            "metadata": {"__metadata:topic": "test"},
        }
        for i in range(4)
    ]
    config["rubrics"] = ["The answer is accurate."]
    config["evaluation"] = {
        "mode": "matrix",
        "prompts": ["default", "other"],
        "sources": ["default", "other"],
        "models": ["default", "second"],
        "judge_model": "judge",
        "top_k": 2,
    }
    saved = client.put(
        f"/api/bots/{bot['id']}", json={"version": bot["version"], "config": config}
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def test_matrix_cross_product_separate_judge_and_snapshot(hosted):
    bot = matrix_bot(hosted)
    settings, store, runtime, client = hosted[:4]
    requested = []
    retrieval = []
    runtime.retrieve = lambda docs, profile, question, top_k: (
        retrieval.append((profile["key"], profile["knowledgebase"], top_k)) or []
    )

    def completion(messages, preset):
        requested.append(preset)
        return (
            json.dumps({"score": 1, "reason": "Correct"})
            if preset == "judge"
            else "Grounded answer"
        )

    runtime.completion = completion
    base = f"/api/bots/{bot['id']}"
    plan = client.get(base + "/evaluation-plan").json()
    assert (plan["answers"], plan["judges"], plan["total_calls"], plan["limit"]) == (
        32,
        32,
        64,
        240,
    )
    queued = client.post(base + "/evaluate")
    assert queued.status_code == 200, queued.text
    # Edits after enqueue must not change the run.
    bot["config"]["scenarios"] = [{"input": "Later draft", "expected": ""}]
    assert (
        client.put(
            base, json={"version": bot["version"], "config": bot["config"]}
        ).status_code
        == 200
    )
    run_one(store, settings, runtime)
    with store.transaction() as db:
        job = db.get(Job, queued.json()["id"])
        assert job.status == "succeeded", job.result
        rows = job.result["rows"]
        assert len(rows) == 32 and all(row["passed"] for row in rows)
        assert (
            len(
                {
                    (r["input"], r["prompt_key"], r["source_profile"], r["model"])
                    for r in rows
                }
            )
            == 32
        )
        assert {r["judge_model"] for r in rows} == {"judge"}
        assert rows[0]["metadata"] == {"__metadata:topic": "test"}
        assert db.get(Organization, "school").calls == 64
    assert requested.count("judge") == 32
    assert requested.count("default") == requested.count("second") == 16
    assert set(retrieval) == {
        ("default", "default", 2),
        ("default", "other", 2),
        ("other", "default", 2),
        ("other", "other", 2),
    }


def test_cap_and_unknown_models_do_not_spend(hosted):
    bot = matrix_bot(hosted)
    settings, store, _, client = hosted[:4]
    settings.evaluation_max_calls = 63
    response = client.post(f"/api/bots/{bot['id']}/evaluate")
    assert response.status_code == 422 and "64" in response.text
    with store.transaction() as db:
        assert db.get(Organization, "school").calls == 0
    bot["config"]["evaluation"]["models"] = ["unconfigured"]
    assert (
        client.put(
            f"/api/bots/{bot['id']}",
            json={"version": bot["version"], "config": bot["config"]},
        ).status_code
        == 422
    )


def test_one_model_failure_does_not_lose_other_matrix_rows(hosted):
    bot = matrix_bot(hosted)
    settings, store, runtime, client = hosted[:4]

    def completion(messages, preset):
        if preset == "second":
            raise RuntimeError("SECRET_PROVIDER_DETAIL")
        return '{"score":1,"reason":"ok"}' if preset == "judge" else "Grounded answer"

    runtime.completion = completion
    queued = client.post(f"/api/bots/{bot['id']}/evaluate").json()
    run_one(store, settings, runtime)
    with store.transaction() as db:
        job = db.get(Job, queued["id"])
        assert job.status == "succeeded"
        assert len(job.result["rows"]) == 32
        assert sum(bool(row["error"]) for row in job.result["rows"]) == 16
        assert "SECRET_PROVIDER_DETAIL" not in json.dumps(job.result)


def test_scenario_csv_metadata_and_workspace_roundtrip(hosted):
    bot = matrix_bot(hosted)
    client = hosted[3]
    base = f"/api/bots/{bot['id']}"
    csv = 'question,gold_label,__description,__metadata:topic\n"Why, now?",icontains:answer,Timing,procedure\n'
    rows = client.post(base + "/scenarios/import", json={"csv": csv}).json()[
        "scenarios"
    ]
    assert rows[0]["description"] == "Timing" and rows[0]["metadata"] == {
        "__metadata:topic": "procedure"
    }
    bot["config"]["scenarios"] = rows
    assert (
        client.put(
            base, json={"version": bot["version"], "config": bot["config"]}
        ).status_code
        == 200
    )
    downloaded = client.get(base + "/scenarios.csv")
    assert "__metadata:topic" in downloaded.text and '"Why, now?"' in downloaded.text
    workspace = client.get(base + "/workspace").text
    other = new_bot(hosted)
    imported = client.post(
        f"/api/bots/{other['id']}/workspace", json={"version": 1, "yaml": workspace}
    )
    assert imported.status_code == 200, imported.text
    config = imported.json()["bot"]["config"]
    assert config["evaluation"] == bot["config"]["evaluation"]
    assert config["scenarios"] == rows
    assert (
        hosted[4]("bob").post(base + "/scenarios/import", json={"csv": csv}).status_code
        == 404
    )


def test_generation_grounded_and_invalid_requests_do_not_spend(hosted):
    from asklit.hosted.models import Document

    bot = new_bot(hosted)
    settings, store, runtime, client = hosted[:4]
    base = f"/api/bots/{bot['id']}"
    body = {"profile": "default", "model": "default", "count": 2}
    assert client.post(base + "/scenarios/generate", json=body).status_code == 422
    with store.transaction() as db:
        assert db.get(Organization, "school").calls == 0
        db.add(
            Document(
                bot_id=bot["id"],
                filename="source.txt",
                knowledgebase="default",
                content_hash="x",
                size=10,
                storage_key="unused",
                embedding_model="fake",
                status="ready",
                chunks=[{"content": "The synthetic color is violet."}],
                embeddings=[[1, 0]],
            )
        )

    def complete(messages, preset):
        assert "synthetic color is violet" in messages[1]["content"]
        return '[{"input":"Color?","__expected":"icontains:violet","__description":"Color test"}]'

    runtime.completion = complete
    generated = client.post(base + "/scenarios/generate", json=body)
    assert generated.status_code == 200, generated.text
    assert generated.json()["scenarios"][0]["expected"] == "icontains:violet"
    assert (
        client.get(base).json()["config"]["scenarios"] == []
    )  # user reviews before saving
    with store.transaction() as db:
        assert db.get(Organization, "school").calls == 1
    assert (
        client.post(
            base + "/scenarios/generate", json={**body, "model": "arbitrary"}
        ).status_code
        == 422
    )
