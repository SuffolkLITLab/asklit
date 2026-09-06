"""End-to-end checks of the scaffolder's single workflow via Streamlit's AppTest."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

sys.modules.setdefault("litellm", SimpleNamespace())

from asklit.scaffold import access, step_chat
from asklit.scaffold.ui import BILLED_STEPS, STEPS

STEP_LABELS = [label for _key, label in STEPS]


@pytest.fixture(autouse=True)
def isolated_streamlit_secrets():
    """Keep AppTest's secrets loading out of the rest of the suite.

    ``st.secrets`` is a process-wide singleton, so running the app here would
    otherwise leave this machine's real secrets.toml loaded for later tests.
    """
    st.secrets._reset()
    yield
    st.secrets._reset()


def run_app(step_label=None):
    app = AppTest.from_file("scaffold.py", default_timeout=120)
    app.run()
    if step_label:
        app.sidebar.radio[0].set_value(step_label).run()
    return app


def test_every_step_renders_without_error():
    for label in STEP_LABELS:
        app = run_app(label)
        assert not app.exception, f"{label} raised {[e.value for e in app.exception]}"
        assert app.header, f"{label} rendered no header"


def test_the_workflow_is_a_single_five_step_flow():
    app = run_app()

    assert [radio.label for radio in app.sidebar.radio] == ["Steps"]
    assert app.sidebar.radio[0].options == [
        "1. Knowledge",
        "2. Prompt",
        "3. Chat",
        "4. Evaluate",
        "5. Export",
    ]


def test_next_button_advances_and_the_last_step_has_none():
    app = run_app("1. Knowledge")
    next_buttons = [b for b in app.button if b.label.startswith("Next")]
    assert next_buttons[0].label == "Next: 2. Prompt"
    next_buttons[0].click().run()
    assert app.header[0].value == "Write a prompt"

    app = run_app("5. Export")
    assert not [b for b in app.button if b.label.startswith("Next")]


def test_export_step_carries_every_deployment_setting():
    app = run_app("5. Export")
    labels = {widget.label for widget in app.text_input}
    labels |= {widget.label for widget in app.text_area}
    labels |= {widget.label for widget in app.selectbox}
    labels |= {widget.label for widget in app.checkbox}

    # Settings that used to live only in the removed Builder mode.
    assert "App title" in labels
    assert "Provider" in labels
    assert "Who can access the chat?" in labels
    assert "Disable admin backend" in labels
    assert "Homepage URL" in labels
    assert "Repository Name" in labels
    assert "Approved model names (comma-separated)" in labels
    assert {"Upload logo", "Upload favicon"} <= {
        widget.label for widget in app.get("file_uploader")
    }


def test_prompt_step_exposes_the_advanced_pairing_fields():
    app = run_app("2. Prompt")
    labels = {widget.label for widget in app.text_input}
    labels |= {widget.label for widget in app.text_area}

    assert "Prompt name" in labels
    assert "Knowledge-base name" in labels
    assert "System prompt" in labels
    assert "Conversation starters" in labels
    assert "YAML key" in labels


@pytest.mark.parametrize("step_label", ["3. Chat", "4. Evaluate"])
def test_billed_steps_are_gated_when_a_password_is_configured(monkeypatch, step_label):
    monkeypatch.setattr(access, "configured_password", lambda: "class-2026")
    monkeypatch.setattr(access, "configured_password_hash", lambda: None)

    app = run_app(step_label)

    assert not app.exception
    assert any("password protected" in info.value for info in app.info)
    assert app.text_input[0].label == "Scaffolder access password"


@pytest.mark.parametrize("step_label", ["1. Knowledge", "2. Prompt", "5. Export"])
def test_free_steps_stay_open_when_a_password_is_configured(monkeypatch, step_label):
    monkeypatch.setattr(access, "configured_password", lambda: "class-2026")
    monkeypatch.setattr(access, "configured_password_hash", lambda: None)

    app = run_app(step_label)

    assert not app.exception
    assert not any(
        widget.label == "Scaffolder access password" for widget in app.text_input
    )


def test_correct_password_unlocks_the_billed_steps(monkeypatch):
    monkeypatch.setattr(access, "configured_password", lambda: "class-2026")
    monkeypatch.setattr(access, "configured_password_hash", lambda: None)

    app = run_app("3. Chat")
    app.text_input[0].set_value("class-2026").run()

    assert app.header[0].value == "Try the advisor"


def test_wrong_password_is_rejected(monkeypatch):
    monkeypatch.setattr(access, "configured_password", lambda: "class-2026")
    monkeypatch.setattr(access, "configured_password_hash", lambda: None)

    app = run_app("3. Chat")
    app.text_input[0].set_value("guess").run()

    assert any("not correct" in error.value for error in app.error)
    assert app.header[0].value == "Chat preview"


def test_no_password_configured_leaves_every_step_open():
    for label in STEP_LABELS:
        app = run_app(label)
        assert not any(
            widget.label == "Scaffolder access password" for widget in app.text_input
        )


def test_billed_steps_match_the_steps_that_call_a_model():
    assert BILLED_STEPS == {"chat", "evaluate"}


def test_preview_chat_stops_at_the_turn_limit(monkeypatch):
    monkeypatch.setattr(step_chat, "preview_chat_turn_limit", lambda: 2)
    app = AppTest.from_file("scaffold.py", default_timeout=120)
    app.run()
    app.sidebar.radio[0].set_value("3. Chat").run()
    app.session_state["preview_chat_messages"] = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a", "model": "m"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "b", "model": "m"},
    ]
    app.run()

    assert any("2 questions per conversation" in w.value for w in app.warning)
    assert not app.chat_input


def test_preview_chat_reports_remaining_questions(monkeypatch):
    monkeypatch.setattr(step_chat, "preview_chat_turn_limit", lambda: 12)
    app = run_app("3. Chat")

    assert any("12 of 12 preview questions remaining" in c.value for c in app.caption)
    assert app.chat_input


def _result(model, passed, score, tokens, judge_tokens):
    return {
        "run_id": "r",
        "prompt_label": "Housing",
        "prompt_key": "housing",
        "knowledgebase_label": "Housing",
        "knowledgebase": "housing",
        "model": model,
        "provider": "openai",
        "scenario": "Notice",
        "input": "What notice is required?",
        "expected": "icontains:notice",
        "answer": "Fourteen days notice.",
        "grader": "gold label + model rubric",
        "judge_model": "judge-model",
        "passed": passed,
        "score": score,
        "grade_reason": "Gold label (icontains): pass · Model rubric: good",
        "error": None,
        "failure_stage": None,
        "elapsed": 1.0,
        "judge_elapsed": 0.4,
        "tokens": tokens,
        "judge_tokens": judge_tokens,
        "sources": [{"filename": "guide.pdf", "page": 3, "content": "Fourteen days."}],
    }


def seeded_results_app():
    app = run_app("4. Evaluate")
    app.session_state["experiment_results"] = [
        _result("m1", True, 0.9, 500, 120),
        _result("m2", False, 0.0, 400, 100),
    ]
    app.session_state["experiment_run_settings"] = {
        "provider": "openai",
        "top_k": 5,
        "mode": "Prompt × model matrix",
        "judge_model": "judge-model",
    }
    app.run()
    return app


def test_results_report_judge_tokens_in_the_cost_total():
    app = seeded_results_app()
    metrics = {metric.label: metric.value for metric in app.metric}

    assert not app.exception
    assert metrics["Runs"] == "2"
    assert metrics["Pass rate"] == "50%"
    # 500 + 120 + 400 + 100: the judge's own calls are not hidden from the total.
    assert metrics["Approx. tokens"] == "1,120"


def test_results_show_the_settings_that_produced_them():
    app = seeded_results_app()

    assert any(
        "provider **openai**" in caption.value and "5 retrieved passage(s)" in caption.value
        for caption in app.caption
    )


def test_carrying_a_result_forward_updates_the_exported_configuration():
    app = seeded_results_app()
    apply_buttons = [
        button
        for button in app.button
        if button.label == "Use this prompt and model for the exported app"
    ]
    assert apply_buttons, "the winning configuration cannot be carried forward"
    apply_buttons[0].click().run()

    model_config = app.session_state["app_config"]["model"]
    assert model_config["name"] == "m1"  # the higher pass rate
    assert model_config["provider"] == "openai"
    assert any("will default to" in success.value for success in app.success)


def _next_button(app):
    return [button for button in app.button if button.label.startswith("Next")][0]


def _action_button(app, label):
    return [button for button in app.button if button.label == label][0]


def test_prompt_edits_survive_a_click_that_leaves_the_step():
    """Typing must reach the workspace even when a button click ends the step.

    Streamlit commits a text area on blur, so clicking a button while the box
    still holds focus races that blur and the edit used to be dropped
    (streamlit/streamlit#8725). Queueing both interactions into one run is the
    closest AppTest gets to that race; the keyed widgets and their on_change
    callbacks are what make the text land in app_config either way.
    """
    app = run_app("2. Prompt")
    app.text_area("prompt_text_0").set_value("Only answer from the uploaded cases.")
    _next_button(app).click()
    app.run()

    assert not app.exception
    assert app.header[0].value != "Write a prompt", "the Next click was swallowed"
    profiles = app.session_state["app_config"]["prompt_profiles"]
    assert profiles[0]["prompt"] == "Only answer from the uploaded cases."


def test_conversation_starters_survive_a_click_that_leaves_the_step():
    app = run_app("2. Prompt")
    app.text_area("prompt_starters_0").set_value("What is a continuance?\n\nCan I appeal?")
    _next_button(app).click()
    app.run()

    profiles = app.session_state["app_config"]["prompt_profiles"]
    assert profiles[0]["conversation_starters"] == [
        "What is a continuance?",
        "Can I appeal?",
    ]


def test_the_export_step_edits_the_prompt_instead_of_overwriting_it():
    """Export repeats the prompt fields, so it must not restore stale text."""
    app = run_app("2. Prompt")
    app.text_area("prompt_text_0").set_value("Cite the statute section.").run()
    app.sidebar.radio[0].set_value("5. Export").run()

    assert app.text_area("export_prompt_0").value == "Cite the statute section."

    app.text_area("export_prompt_0").set_value("Cite the statute and the year.").run()
    profiles = app.session_state["app_config"]["prompt_profiles"]
    assert profiles[0]["prompt"] == "Cite the statute and the year."


def test_switching_prompts_does_not_carry_text_between_them():
    app = run_app("2. Prompt")
    app.text_area("prompt_text_0").set_value("First prompt text.").run()
    _action_button(app, "Add another prompt").click().run()
    app.selectbox("prompt_editor_index").set_value(1).run()
    app.text_area("prompt_text_1").set_value("Second prompt text.").run()
    app.selectbox("prompt_editor_index").set_value(0).run()

    assert app.text_area("prompt_text_0").value == "First prompt text."
    profiles = app.session_state["app_config"]["prompt_profiles"]
    assert [profile["prompt"] for profile in profiles] == [
        "First prompt text.",
        "Second prompt text.",
    ]


def test_removing_a_prompt_leaves_the_survivor_showing_its_own_text():
    """Editor keys are index-based, so a removal has to reset them.

    Dropping the first of two profiles shifts the second into index 0. Without
    clearing the keyed widgets, index 0 kept the removed prompt's text on
    screen and the next blur wrote it over the survivor.
    """
    app = run_app("2. Prompt")
    app.text_area("prompt_text_0").set_value("Doomed prompt.").run()
    _action_button(app, "Add another prompt").click().run()
    app.selectbox("prompt_editor_index").set_value(1).run()
    app.text_area("prompt_text_1").set_value("Surviving prompt.").run()

    app.selectbox("prompt_editor_index").set_value(0).run()
    _action_button(app, "Remove this prompt").click().run()

    assert not app.exception
    profiles = app.session_state["app_config"]["prompt_profiles"]
    assert [profile["prompt"] for profile in profiles] == ["Surviving prompt."]
    assert app.text_area("prompt_text_0").value == "Surviving prompt."


def test_a_prompt_typed_in_the_editor_reaches_the_deployed_yaml(tmp_path):
    """Reproduce the classroom report: the exported YAML kept the stock prompt.

    A student typed a system prompt, could not press the Ctrl/Cmd+Enter the
    caption asked for, and moved on. The keyless text area dropped the edit, so
    app_config still held DEFAULT_PROMPT_TEXT — which then flowed into the
    evaluation and into prompts/*.yml, and the exported app shipped with
    "You are a helpful assistant."
    """
    import yaml
    from asklit.db import init_db
    from asklit.scaffold import bundle as bundle_module
    from asklit.scaffold.config import DEFAULT_PROMPT_TEXT

    student_prompt = (
        "You are a Massachusetts housing advisor. Answer only from the "
        "retrieved passages and say so when they are silent."
    )

    app = run_app("2. Prompt")
    app.text_area("prompt_text_0").set_value(student_prompt)
    _next_button(app).click()
    app.run()

    profiles = app.session_state["app_config"]["prompt_profiles"]
    assert profiles[0]["prompt"] == student_prompt, "the editor dropped the prompt"
    assert profiles[0]["prompt"] != DEFAULT_PROMPT_TEXT

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    init_db(str(session_dir / "app.sqlite3"))
    bundle_dir = Path(
        bundle_module.create_bundle(
            app.session_state["app_config"], str(session_dir)
        )
    )

    exported = yaml.safe_load(
        (bundle_dir / "prompts" / f"{profiles[0]['key']}.yml").read_text()
    )
    assert exported["prompt"] == student_prompt


def test_the_save_button_commits_the_prompt_on_its_own():
    """The button must store the text without relying on a blur first.

    This is the affordance for a learner who cannot press Ctrl/Cmd+Enter, so
    it re-reads the fields rather than trusting that on_change already fired.
    """
    app = run_app("2. Prompt")
    app.text_area("prompt_text_0").set_value("Answer only from the record.")
    _action_button(app, "Save prompt").click()
    app.run()

    assert not app.exception
    profiles = app.session_state["app_config"]["prompt_profiles"]
    assert profiles[0]["prompt"] == "Answer only from the record."
    assert any("Prompt saved." in success.value for success in app.success)


@pytest.mark.parametrize("step_label", ["4. Evaluate", "5. Export"])
def test_an_unwritten_prompt_is_called_out_before_it_costs_anything(step_label):
    """A prompt left at the stock text used to sail silently into both steps."""
    app = run_app(step_label)

    assert any(
        "stock prompt" in warning.value for warning in app.warning
    ), f"{step_label} did not flag the default prompt"


@pytest.mark.parametrize("step_label", ["4. Evaluate", "5. Export"])
def test_a_written_prompt_is_not_flagged(step_label):
    app = run_app("2. Prompt")
    app.text_area("prompt_text_0").set_value("Answer only from the record.").run()
    app.sidebar.radio[0].set_value(step_label).run()

    assert not any("stock prompt" in warning.value for warning in app.warning)
