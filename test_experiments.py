from types import SimpleNamespace

from asklit.experiments import (
    build_experiment_matrix,
    build_experiment_messages,
    parse_model_names,
    response_text,
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
