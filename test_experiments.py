from types import SimpleNamespace

from asklit.experiments import (
    build_evaluation_matrix,
    build_experiment_matrix,
    build_experiment_messages,
    build_rubric_judge_messages,
    combine_grades,
    deterministic_grade,
    evaluate_expected,
    grader_label,
    is_model_rubric,
    parse_generated_scenarios,
    parse_model_names,
    parse_rubric_grade,
    parse_scenario_csv,
    resolve_rubric_rules,
    response_text,
    scenarios_to_csv,
)

PROFILES = [
    {
        "key": "housing",
        "label": "Housing",
        "knowledgebase": "housing-kb",
        "prompt": "Housing prompt",
        "connected_files": [],
    },
    {
        "key": "benefits",
        "label": "Benefits",
        "knowledgebase": "benefits-kb",
        "prompt": "Benefits prompt",
        "connected_files": [],
    },
]


def test_experiment_matrix_builds_cartesian_product():
    matrix = build_experiment_matrix(
        PROFILES,
        ["housing", "benefits"],
        ["benefits"],
        "model-a, model-b",
    )

    assert len(matrix) == 4
    assert {item["prompt"]["key"] for item in matrix} == {"housing", "benefits"}
    assert {item["knowledgebase"]["key"] for item in matrix} == {"benefits"}
    assert {item["model"] for item in matrix} == {"model-a", "model-b"}


def test_model_names_are_trimmed_and_deduplicated():
    assert parse_model_names("model-a\nmodel-b, model-a") == ["model-a", "model-b"]


def test_experiment_messages_use_selected_prompt_and_context():
    messages = build_experiment_messages(
        "Selected prompt",
        "What are my rights?",
        [{"content": "A" * 100}],
    )

    assert messages[0]["role"] == "system"
    assert "Selected prompt" in messages[0]["content"]
    assert "A" * 100 in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "What are my rights?"}


def test_response_text_supports_object_and_dict_responses():
    object_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Object answer"))]
    )
    dict_response = {"choices": [{"message": {"content": "Dict answer"}}]}

    assert response_text(object_response) == "Object answer"
    assert response_text(dict_response) == "Dict answer"


def test_promptfoo_style_csv_and_aliases_are_normalized():
    rows = parse_scenario_csv(
        "question,__expected,__description,__metadata:topic\n"
        '"Where?","icontains:Boston","Location","housing"\n'
    )

    assert rows == [
        {
            "input": "Where?",
            "__expected": "icontains:Boston",
            "__description": "Location",
            "__metadata:topic": "housing",
        }
    ]
    assert (
        parse_scenario_csv("query,gold_label\nWhat?,Answer\n")[0]["__expected"]
        == "Answer"
    )
    assert (
        parse_scenario_csv("question,expectedAnswer\nWhy?,Because\n")[0]["__expected"]
        == "Because"
    )


def test_scenario_csv_round_trip_preserves_promptfoo_columns():
    original = [
        {
            "input": "What?",
            "__expected": "contains:answer",
            "__description": "Basic",
            "__metadata:topic": "demo",
        }
    ]

    assert parse_scenario_csv(scenarios_to_csv(original)) == original


def test_supported_gold_assertions_are_transparent_and_deterministic():
    assert evaluate_expected("The Answer is Boston.", "icontains:boston")["passed"]
    assert evaluate_expected("red and blue", "contains-all:red,blue")["passed"]
    assert not evaluate_expected("red", "contains-all:red,blue")["passed"]
    unsupported = evaluate_expected("anything", "judge-unknown:Be correct")
    assert unsupported["passed"] is None
    assert unsupported["reason"] == "Unsupported assertion: judge-unknown"


def test_model_rubric_assertion_is_explicit_and_parseable():
    expected = "llm-rubric:Explains the next step and acknowledges uncertainty"
    assert is_model_rubric(expected)
    pending = evaluate_expected("Any answer", expected)
    assert pending["passed"] is None
    assert pending["reason"] == "Model rubric (judge required)"
    assert parse_rubric_grade(
        '{"score": 0.8, "passed": true, "reason": "Meets both criteria"}'
    ) == {"passed": True, "score": 0.8, "reason": "Model rubric: Meets both criteria"}
    assert parse_rubric_grade(
        '{"score": 0.6, "passed": true, "reason": "Partial"}'
    )["passed"] is False
    assert parse_rubric_grade(
        '{"score": 0.9, "narrative": "Clear and grounded"}'
    )["reason"] == "Model rubric: Clear and grounded"


def test_rubric_judge_messages_include_all_grading_inputs():
    messages = build_rubric_judge_messages(
        "What should I do?", "Tell the landlord in writing.", "Names a practical next step"
    )
    assert "What should I do?" in messages[1]["content"]
    assert "Tell the landlord" in messages[1]["content"]
    assert "Names a practical next step" in messages[1]["content"]


def test_generated_scenarios_accept_fenced_json():
    rows = parse_generated_scenarios(
        '```json\n[{"input":"Question?","__expected":"contains:Answer"}]\n```'
    )

    assert rows[0]["input"] == "Question?"
    assert rows[0]["__expected"] == "contains:Answer"


def test_evaluation_matrix_crosses_scenarios_prompts_and_models():
    matrix = build_evaluation_matrix(
        PROFILES,
        ["housing", "benefits"],
        ["housing"],
        ["model-a", "model-b"],
        [
            {"input": "First", "__expected": "one"},
            {"input": "Second", "__expected": "two"},
        ],
    )

    assert len(matrix) == 8
    assert {item["scenario"]["input"] for item in matrix} == {"First", "Second"}


def test_experiment_messages_include_prior_turns_like_the_deployed_chat():
    messages = build_experiment_messages(
        "You help with housing.",
        "What about the second option?",
        [{"content": "Tenants may request repairs in writing. " * 5}],
        chat_history=[
            {"role": "user", "content": "What are my options?"},
            {"role": "assistant", "content": "You have two options."},
            {"role": "system", "content": "should be dropped"},
        ],
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert messages[1]["content"] == "What are my options?"
    assert messages[-1]["content"] == "What about the second option?"


def test_experiment_messages_without_history_stay_single_turn():
    messages = build_experiment_messages("Prompt", "Question", [])

    assert [message["role"] for message in messages] == ["system", "user"]


def test_rubric_judge_sees_retrieved_passages_and_the_pass_threshold():
    messages = build_rubric_judge_messages(
        "What notice is required?",
        "Fourteen days.",
        "- Stays grounded in the retrieved passages",
        context_chunks=[
            {"content": "Fourteen days notice is required.", "metadata": {"page_number": 3}}
        ],
    )

    assert "RETRIEVED CONTEXT" in messages[1]["content"]
    assert "page 3" in messages[1]["content"]
    assert "0.70" in messages[0]["content"]
    assert "Judge groundedness only against the RETRIEVED CONTEXT" in messages[0]["content"]


def test_judge_is_told_not_to_guess_when_no_context_was_retrieved():
    messages = build_rubric_judge_messages("Q", "A", "- Be clear")

    assert "RETRIEVED CONTEXT" not in messages[1]["content"]
    assert "do not guess whether claims are grounded" in messages[0]["content"]


def test_shared_rules_combine_with_a_row_rubric():
    rules = resolve_rubric_rules(
        ["Answers in plain language"], "llm-rubric:Names the filing deadline"
    )

    assert rules == ["Answers in plain language", "Names the filing deadline"]


def test_shared_rules_apply_to_rows_with_a_deterministic_label():
    assert resolve_rubric_rules(["Be clear"], "icontains:notice") == ["Be clear"]
    assert resolve_rubric_rules([], "icontains:notice") == []


def test_deterministic_grade_is_skipped_for_rubric_rows():
    assert deterministic_grade("anything", "llm-rubric:Be clear") is None
    assert deterministic_grade("anything", "") is None
    assert deterministic_grade("says notice", "icontains:notice")["passed"] is True


def test_both_graders_must_pass_when_both_are_configured():
    gold_pass = {"passed": True, "score": 1.0, "reason": "icontains"}
    gold_fail = {"passed": False, "score": 0.0, "reason": "icontains"}
    rubric_pass = {"passed": True, "score": 0.9, "reason": "Model rubric: good"}
    rubric_fail = {"passed": False, "score": 0.2, "reason": "Model rubric: vague"}

    assert combine_grades(gold_pass, rubric_pass)["passed"] is True
    assert combine_grades(gold_pass, rubric_fail)["passed"] is False
    assert combine_grades(gold_fail, rubric_pass)["passed"] is False
    # The stricter of the two scores is reported, never just the judge's.
    assert combine_grades(gold_pass, rubric_pass)["score"] == 0.9


def test_a_single_grader_is_reported_unchanged():
    gold = {"passed": True, "score": 1.0, "reason": "icontains"}

    assert combine_grades(gold, None) == gold
    assert combine_grades(None, gold) == gold
    assert combine_grades(None, None)["passed"] is None


def test_grader_label_names_the_graders_that_decided_the_row():
    grade = {"passed": True, "score": 1.0, "reason": "r"}

    assert grader_label(grade, grade) == "gold label + model rubric"
    assert grader_label(None, grade) == "model rubric"
    assert grader_label(grade, None) == "gold label"
    assert grader_label(None, None) == "ungraded"
