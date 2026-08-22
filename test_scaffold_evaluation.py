import sys
from types import SimpleNamespace

sys.modules.setdefault("litellm", SimpleNamespace())

from asklit.scaffold import evaluation


def make_matrix(expected):
    profile = {
        "key": "housing",
        "label": "Housing",
        "knowledgebase": "housing",
        "prompt": "Help with housing.",
        "connected_files": [],
    }
    return [
        {
            "prompt": profile,
            "knowledgebase": profile,
            "model": "test-model",
            "scenario": {
                "input": "What notice is required?",
                "__expected": expected,
                "__description": "Notice",
            },
        }
    ]


def run(monkeypatch, expected, shared_rubrics, answer, judge_payload):
    logged = []
    monkeypatch.setattr(
        evaluation, "log_ai_call_event", lambda **kwargs: logged.append(kwargs) or None
    )

    def call_model(messages, **kwargs):
        is_judge = "evaluation judge" in messages[0]["content"]
        content = judge_payload if is_judge else answer
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    results = evaluation.run_evaluation(
        make_matrix(expected),
        run_id="run-1",
        provider="openai",
        judge_model="judge-model",
        shared_rubrics=shared_rubrics,
        top_k=3,
        db_path=":memory:",
        chroma_path="/tmp/does-not-matter",
        document_labels={"doc-1": "guide.pdf"},
        call_model=call_model,
        retrieve=lambda *args, **kwargs: [
            {"content": "Fourteen days notice is required.", "metadata": {"document_id": "doc-1", "page_number": 2}}
        ],
    )
    return results[0], logged


def test_shared_rubric_does_not_discard_a_row_gold_label(monkeypatch):
    # The answer satisfies the rubric but misses the required phrase, so the
    # combined outcome must fail rather than silently reporting the judge's pass.
    result, _logged = run(
        monkeypatch,
        expected="icontains:fourteen days",
        shared_rubrics=["Answers in plain language"],
        answer="You must be given two weeks of warning.",
        judge_payload='{"score": 0.95, "reason": "Clear and plain."}',
    )

    assert result["passed"] is False
    assert result["grader"] == "gold label + model rubric"
    assert "icontains" in result["grade_reason"]
    assert "Clear and plain" in result["grade_reason"]


def test_row_passes_only_when_both_graders_pass(monkeypatch):
    result, _logged = run(
        monkeypatch,
        expected="icontains:fourteen days",
        shared_rubrics=["Answers in plain language"],
        answer="Fourteen days notice is required before a hearing.",
        judge_payload='{"score": 0.9, "reason": "Grounded."}',
    )

    assert result["passed"] is True
    assert result["score"] == 0.9


def test_judge_sees_retrieved_context(monkeypatch):
    captured = {}
    monkeypatch.setattr(evaluation, "log_ai_call_event", lambda **kwargs: None)

    def call_model(messages, **kwargs):
        if "evaluation judge" in messages[0]["content"]:
            captured["judge_prompt"] = messages[1]["content"]
            content = '{"score": 0.8, "reason": "ok"}'
        else:
            content = "Fourteen days."
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    evaluation.run_evaluation(
        make_matrix("llm-rubric:Stays grounded in the retrieved passages"),
        run_id="run-2",
        provider="openai",
        judge_model="judge-model",
        shared_rubrics=[],
        top_k=3,
        db_path=":memory:",
        chroma_path="/tmp/x",
        document_labels={},
        call_model=call_model,
        retrieve=lambda *args, **kwargs: [
            {"content": "Fourteen days notice is required.", "metadata": {"page_number": 2}}
        ],
    )

    assert "RETRIEVED CONTEXT" in captured["judge_prompt"]
    assert "Fourteen days notice is required." in captured["judge_prompt"]


def test_judge_call_is_logged_and_counted_separately(monkeypatch):
    result, logged = run(
        monkeypatch,
        expected="",
        shared_rubrics=["Answers in plain language"],
        answer="Two weeks of warning.",
        judge_payload='{"score": 0.9, "reason": "Plain."}',
    )

    sources = [entry["source"] for entry in logged]
    assert evaluation.EXPERIMENT_SOURCE in sources
    assert evaluation.JUDGE_SOURCE in sources
    assert result["judge_tokens"] > 0


def test_failed_judge_marks_the_row_ungraded(monkeypatch):
    result, logged = run(
        monkeypatch,
        expected="",
        shared_rubrics=["Answers in plain language"],
        answer="Two weeks of warning.",
        judge_payload="not json at all",
    )

    assert result["passed"] is None
    assert result["failure_stage"] == "judge"
    assert any(
        entry["source"] == evaluation.JUDGE_SOURCE and entry["status"] == "failed"
        for entry in logged
    )


def test_summary_and_best_configuration():
    results = [
        {"prompt_label": "A", "model": "m1", "passed": True},
        {"prompt_label": "A", "model": "m1", "passed": False},
        {"prompt_label": "B", "model": "m1", "passed": True},
        {"prompt_label": "B", "model": "m1", "passed": True},
    ]
    summary = evaluation.summarize_by_prompt_and_model(results)

    assert [row["Pass rate"] for row in summary] == [0.5, 1.0]
    best = evaluation.best_configuration(summary)
    assert (best["Prompt"], best["Model"]) == ("B", "m1")


def test_judge_call_count_matches_the_cost_preview():
    matrix = make_matrix("icontains:notice") + make_matrix("llm-rubric:Be clear")

    assert evaluation.count_judge_calls(matrix, []) == 1
    assert evaluation.count_judge_calls(matrix, ["Shared rule"]) == 2
