from types import SimpleNamespace

from asklit.experiments import (
    build_evaluation_matrix,
    build_experiment_matrix,
    build_experiment_messages,
    build_rubric_judge_messages,
    evaluate_expected,
    is_model_rubric,
    parse_generated_scenarios,
    parse_model_names,
    parse_rubric_grade,
    parse_scenario_csv,
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
