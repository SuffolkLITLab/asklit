"""Step 4: gold-labeled scenarios, matrix runs, and grading."""

import uuid

import pandas as pd
import streamlit as st

from asklit.config import get_api_key, get_base_url
from asklit.experiments import (
    RUBRIC_PASS_THRESHOLD,
    build_evaluation_matrix,
    normalize_scenario_rows,
    parse_generated_scenarios,
    parse_model_names,
    parse_scenario_csv,
    response_text,
    scenarios_to_csv,
)
from asklit.llm import call_llm, get_allowed_models
from asklit.scaffold.config import (
    ensure_model_defaults,
    normalize_prompt_profiles,
    provider_options,
)
from asklit.scaffold.endpoints import (
    get_endpoint_model_choices,
    host_runnable_models,
    render_endpoint_model_status,
)
from asklit.scaffold.evaluation import (
    MAX_MATRIX_CALLS,
    best_configuration,
    count_judge_calls,
    run_evaluation,
    summarize_by_prompt_and_model,
)
from asklit.scaffold.ui import session_paths
from asklit.scaffold.knowledge import (
    get_document_labels,
    get_knowledgebase_sample,
    knowledgebase_document_counts,
)

# Semi-transparent so the same fill stays readable in light and dark themes.
OUTCOME_STYLES = {
    "Pass": "background-color: rgba(46, 125, 50, 0.18)",
    "Fail": "background-color: rgba(198, 40, 40, 0.18)",
    "Error": "background-color: rgba(239, 108, 0, 0.18)",
}
RESULT_COLUMN_ORDER = [
    "Outcome",
    "Scenario",
    "Input",
    "Prompt",
    "Knowledge base",
    "Model",
    "Grader",
    "Judge model",
    "Gold label",
    "Answer",
    "Score",
    "Grade rationale",
    "Latency (s)",
    "Approx. tokens",
    "Sources",
    "Error",
]


def _render_provider_controls(model_config):
    """Pick the provider and report whether this host can actually run it."""
    configured_provider = model_config.get("provider", "openai")
    options = provider_options(configured_provider, include_azure=True)
    provider = st.selectbox(
        "Provider",
        options,
        index=options.index(configured_provider),
        key="lab_provider",
    )
    configured_endpoint_url = (
        str(model_config.get("base_url", "")).strip()
        if provider == configured_provider and provider in {"openai", "azure_apim"}
        else ""
    )
    trusted_endpoint_url = str(get_base_url(provider) or "").rstrip("/")
    untrusted_custom_endpoint = bool(
        configured_endpoint_url
        and (
            provider == "openai"
            or not trusted_endpoint_url
            or configured_endpoint_url.rstrip("/") != trusted_endpoint_url
        )
    )
    provider_ready = bool(get_api_key(provider))
    if provider == "azure_apim":
        provider_ready = provider_ready and bool(get_base_url(provider))
    if untrusted_custom_endpoint:
        provider_ready = False
        st.warning(
            "Experiments against a user-supplied endpoint are disabled in the "
            "public scaffolder so its central API key is never sent to that host. "
            "The generated app will use the endpoint with your own key."
        )
    elif not provider_ready:
        st.error(
            f"The scaffolder host has no usable {provider} credentials. "
            "Choose the provider configured by the scaffolder administrator."
        )
    return provider, provider_ready, untrusted_custom_endpoint


def _render_shared_rubrics():
    """Collect rubric rules that apply to every scenario in the set."""
    with st.expander("Advanced: shared rules for every scenario", expanded=False):
        st.markdown(
            "Add one quality rule per line below. The selected judge model reads "
            "each question, the passages that were retrieved, the generated "
            "answer, and all shared rules, then assigns a score from 0 to 1. "
            f"A score of {RUBRIC_PASS_THRESHOLD:.2f} or higher passes."
        )
        st.code(
            "Explains the next practical step, stays grounded in the retrieved "
            "passages, and says when they do not provide enough information",
            language="text",
        )
        st.markdown(
            "Use exact or `icontains:` labels when a specific phrase must appear. "
            "Use a rubric when equivalent wording should receive credit. When a "
            "row has **both** a gold label and shared rules, it passes only if "
            "both graders pass. Rubric grading makes an additional model call for "
            "every scenario × prompt × model combination, so start with a small "
            "set and review the judge rationale before trusting the pass rate."
        )
        shared_rubrics = st.text_area(
            "Shared rubrics (one rule per line)",
            value="\n".join(st.session_state.get("evaluation_rubrics", [])),
            key="evaluation_shared_rubrics",
            height=100,
            help=(
                "Advanced: these rules apply to every scenario in this evaluation. "
                "Leave blank to use only each row's Gold label column."
            ),
        )
        st.session_state.evaluation_rubrics = [
            line.strip() for line in shared_rubrics.splitlines() if line.strip()
        ]
    return st.session_state.evaluation_rubrics


def _render_scenario_editor():
    """Edit, upload, or download the gold-labeled scenario set."""
    if "evaluation_scenarios" not in st.session_state:
        st.session_state.evaluation_scenarios = [
            {
                "input": "What is the most important fact a user should know?",
                "__expected": "",
                "__description": "Core knowledge",
            }
        ]

    uploaded_scenarios = st.file_uploader(
        "Upload scenario CSV", type=["csv"], key="evaluation_scenario_upload"
    )
    if uploaded_scenarios is not None:
        signature = (uploaded_scenarios.name, uploaded_scenarios.size)
        if st.session_state.get("evaluation_upload_signature") != signature:
            try:
                st.session_state.evaluation_scenarios = parse_scenario_csv(
                    uploaded_scenarios.getvalue()
                )
                st.session_state.evaluation_upload_signature = signature
                st.session_state.evaluation_editor_version = (
                    st.session_state.get("evaluation_editor_version", 0) + 1
                )
                st.success(
                    f"Loaded {len(st.session_state.evaluation_scenarios)} scenarios."
                )
            except (UnicodeDecodeError, ValueError) as exc:
                st.error(str(exc))

    scenario_frame = pd.DataFrame(st.session_state.evaluation_scenarios)
    for required_column in ("input", "__expected", "__description"):
        if required_column not in scenario_frame:
            scenario_frame[required_column] = ""
    edited_frame = st.data_editor(
        scenario_frame,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_order=["input", "__expected", "__description"],
        column_config={
            "input": st.column_config.TextColumn("Input", width="large", required=True),
            "__expected": st.column_config.TextColumn(
                "Gold label / __expected",
                width="large",
                help=(
                    "Plain text means exact match. Also supports contains:, icontains:, "
                    "contains-any:, and contains-all:. Leave this blank when the "
                    "advanced shared-rules panel should grade every row."
                ),
            ),
            "__description": st.column_config.TextColumn("Description", width="medium"),
        },
        key=(
            f"evaluation_scenario_editor_"
            f"{st.session_state.get('evaluation_editor_version', 0)}"
        ),
    )
    st.session_state.evaluation_scenarios = normalize_scenario_rows(
        edited_frame.fillna("").to_dict("records")
    )
    st.download_button(
        "Download scenarios as CSV",
        scenarios_to_csv(st.session_state.evaluation_scenarios),
        "asklit-scenarios.csv",
        "text/csv",
    )
    return st.session_state.evaluation_scenarios


def _render_scenario_generator(
    profiles, labels, provider, default_model, allowed_models, provider_ready
):
    """Draft grounded scenarios from the uploaded knowledge base."""
    profile_keys = [profile["key"] for profile in profiles]
    generation_prompt_key = st.selectbox(
        "Prompt for scenario generation",
        profile_keys,
        format_func=lambda key: labels[key],
        key="evaluation_generation_prompt",
    )
    generation_profile = next(
        profile for profile in profiles if profile["key"] == generation_prompt_key
    )
    generation_count = st.slider(
        "Scenarios to generate", 1, 12, 5, key="evaluation_generation_count"
    )
    if not st.button(
        "Generate gold-labeled scenarios",
        disabled=not provider_ready or not default_model,
        key="evaluation_generate",
    ):
        return

    db_path, _chroma_path = session_paths()
    source_sample = get_knowledgebase_sample(
        db_path, generation_profile["knowledgebase"]
    )
    generation_messages = [
        {
            "role": "system",
            "content": (
                "You create concise evaluation datasets. Return only a JSON array. "
                "Each object must contain input, __expected, and __description. "
                "Write realistic user questions answerable from the supplied material. "
                "Use an icontains: gold label containing the shortest decisive phrase "
                "that a correct answer must include. Do not use facts absent from the material."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Create {generation_count} diverse scenarios.\n\n"
                f"SYSTEM PROMPT:\n{generation_profile['prompt']}\n\n"
                f"KNOWLEDGE BASE SAMPLE:\n{source_sample or '[No documents uploaded]'}"
            ),
        },
    ]
    try:
        with st.spinner("Generating scenarios…"):
            generated_response = call_llm(
                generation_messages,
                stream=False,
                max_tokens_override=2000,
                model_override=default_model,
                provider_override=provider,
                extra_allowed_models=allowed_models,
            )
            generated = parse_generated_scenarios(response_text(generated_response))
        if not generated:
            st.error("The model returned no usable scenarios.")
        else:
            st.session_state.evaluation_scenarios = generated
            st.session_state.evaluation_editor_version = (
                st.session_state.get("evaluation_editor_version", 0) + 1
            )
            st.rerun()
    except Exception as exc:
        st.error(f"Scenario generation failed: {exc}")


def _render_run_settings(profiles, labels, model_choices, default_model):
    """Choose the shape of the run and which prompts, bases, and models to use."""
    profile_keys = [profile["key"] for profile in profiles]
    knowledgebase_of = {
        profile["key"]: profile["knowledgebase"] for profile in profiles
    }
    evaluation_mode = st.radio(
        "Evaluation shape",
        ["Single model", "Prompt × model matrix"],
        horizontal=True,
        key="evaluation_mode",
        help=(
            "Single model runs every scenario once. Matrix mode runs every scenario "
            "through every selected prompt, knowledge base, and model."
        ),
    )
    if evaluation_mode == "Single model":
        prompt_keys = [
            st.selectbox(
                "Prompt",
                profile_keys,
                format_func=lambda key: labels[key],
                key="single_evaluation_prompt",
            )
        ]
        knowledgebase_keys = [
            st.selectbox(
                "Knowledge base",
                profile_keys,
                format_func=lambda key: f"{labels[key]} ({knowledgebase_of[key]})",
                key="single_evaluation_knowledgebase",
            )
        ]
        if model_choices:
            model_names = [
                st.selectbox(
                    "Model",
                    model_choices,
                    index=model_choices.index(default_model),
                    key="single_evaluation_model",
                )
            ]
        else:
            model_names = st.text_input(
                "Model", value=default_model, key="single_evaluation_model_text"
            )
    else:
        prompt_keys = st.multiselect(
            "Prompts",
            profile_keys,
            default=[profile_keys[0]],
            format_func=lambda key: labels[key],
            key="matrix_evaluation_prompts",
        )
        knowledgebase_keys = st.multiselect(
            "Knowledge bases",
            profile_keys,
            default=[profile_keys[0]],
            format_func=lambda key: f"{labels[key]} ({knowledgebase_of[key]})",
            key="matrix_evaluation_knowledgebases",
        )
        if model_choices:
            model_names = st.multiselect(
                "Models",
                model_choices,
                default=[default_model],
                key="matrix_evaluation_models",
            )
        else:
            model_names = st.text_area(
                "Models (one per line or comma-separated)",
                value=default_model,
                key="matrix_evaluation_models_text",
            )
    return prompt_keys, knowledgebase_keys, model_names


def _warn_about_unindexed_knowledgebases(profiles, knowledgebase_keys, db_path):
    """Catch the silent failure where a prompt points at an empty knowledge base."""
    counts = knowledgebase_document_counts(db_path)
    empty = sorted(
        {
            profile["knowledgebase"]
            for profile in profiles
            if profile["key"] in knowledgebase_keys
            and not counts.get(profile["knowledgebase"])
        }
    )
    if empty:
        st.warning(
            "No indexed documents belong to: "
            + ", ".join(f"`{name}`" for name in empty)
            + ". These runs will retrieve nothing, and the model will answer from "
            "general knowledge instead. Upload documents in the Knowledge step, or "
            "point the prompt at a knowledge base that has them."
        )


def _render_result_rows(results):
    """Flatten results into the display and download table."""
    rows = []
    for result in results:
        outcome = (
            "Error"
            if result["error"]
            else "Pass"
            if result["passed"] is True
            else "Fail"
            if result["passed"] is False
            else "Not graded"
        )
        rows.append(
            {
                "Run ID": result["run_id"],
                "Outcome": outcome,
                "Scenario": result["scenario"],
                "Input": result["input"],
                "Prompt": result["prompt_label"],
                "Knowledge base": result["knowledgebase_label"],
                "Model": result["model"],
                "Provider": result["provider"],
                "Grader": result.get("grader", "ungraded"),
                "Judge model": result.get("judge_model", ""),
                "Gold label": result["expected"],
                "Answer": result["answer"],
                "Grade rationale": result["grade_reason"],
                "Score": result["score"],
                "Latency (s)": round(
                    result["elapsed"] + result.get("judge_elapsed", 0), 2
                ),
                "Answer latency (s)": round(result["elapsed"], 2),
                "Judge latency (s)": round(result.get("judge_elapsed", 0), 2),
                "Approx. tokens": result["tokens"] + result.get("judge_tokens", 0),
                "Answer tokens": result["tokens"],
                "Judge tokens": result.get("judge_tokens", 0),
                "Sources": ", ".join(
                    f"{source['filename']} p.{source['page']}"
                    for source in result["sources"]
                ),
                "Retrieved context": "\n\n".join(
                    f"[{source['filename']} p.{source['page']}]\n{source['content']}"
                    for source in result["sources"]
                ),
                "Knowledge-base key": result["knowledgebase"],
                "Failure stage": result["failure_stage"] or "",
                "Error": result["error"] or "",
            }
        )
    return rows


def _render_apply_to_export(profiles, summary_rows, provider):
    """Let a winning prompt × model combination become the exported default."""
    choices = [
        f"{row['Prompt']} × {row['Model']}"
        for row in summary_rows
        if row["Pass rate"] is not None
    ]
    if not choices:
        return
    best = best_configuration(summary_rows)
    best_choice = f"{best['Prompt']} × {best['Model']}"
    st.markdown("**Carry a result forward**")
    selection = st.selectbox(
        "Configuration to use in the exported app",
        choices,
        index=choices.index(best_choice),
        key="apply_configuration_choice",
        help="Defaults to the highest pass rate in this run.",
    )
    if not st.button("Use this prompt and model for the exported app"):
        return
    prompt_label, model_name = selection.split(" × ", 1)
    config = st.session_state.app_config
    config["model"]["provider"] = provider
    config["model"]["name"] = model_name
    winning_index = next(
        (
            index
            for index, profile in enumerate(profiles)
            if profile["label"] == prompt_label
        ),
        None,
    )
    if winning_index is not None and winning_index > 0:
        reordered = list(profiles)
        reordered.insert(0, reordered.pop(winning_index))
        config["prompt_profiles"] = normalize_prompt_profiles(reordered)
    st.success(
        f"The exported app will default to **{prompt_label}** on **{model_name}** "
        f"({provider}). Review it in the Export step."
    )


def _render_results(profiles):
    """Show metrics, the prompt × model summary, and the filtered result table."""
    results = st.session_state.get("experiment_results", [])
    if not results:
        return
    st.subheader("3. Results")
    settings = st.session_state.get("experiment_run_settings", {})
    if settings:
        st.caption(
            "This run used provider **{provider}**, {top_k} retrieved passage(s), "
            "and {shape}. Judge model: {judge}.".format(
                provider=settings.get("provider", "—"),
                top_k=settings.get("top_k", "—"),
                shape=settings.get("mode", "—"),
                judge=settings.get("judge_model") or "not used",
            )
        )

    passed = sum(result["passed"] is True for result in results)
    graded = sum(result["passed"] is not None for result in results)
    judge_tokens = sum(result.get("judge_tokens", 0) for result in results)
    total_tokens = sum(
        result["tokens"] + result.get("judge_tokens", 0) for result in results
    )
    metric_columns = st.columns(4)
    metric_columns[0].metric("Runs", len(results))
    metric_columns[1].metric("Graded", graded)
    metric_columns[2].metric("Pass rate", f"{passed / graded:.0%}" if graded else "—")
    metric_columns[3].metric(
        "Approx. tokens",
        f"{total_tokens:,}",
        help=f"Includes {judge_tokens:,} judge tokens." if judge_tokens else None,
    )

    all_table_rows = _render_result_rows(results)
    summary_rows = summarize_by_prompt_and_model(results)
    st.subheader("Pass rate by prompt × model")
    st.dataframe(
        pd.DataFrame(summary_rows),
        hide_index=True,
        width="stretch",
        column_config={"Pass rate": st.column_config.NumberColumn(format="percent")},
    )
    _render_apply_to_export(
        profiles, summary_rows, st.session_state.get("experiment_run_settings", {}).get(
            "provider", ""
        )
    )
    st.download_button(
        "Download all evaluation results as CSV",
        pd.DataFrame(all_table_rows).to_csv(index=False),
        file_name="asklit-evaluation-results.csv",
        mime="text/csv",
    )

    filter_columns = st.columns(3)
    prompt_filter = filter_columns[0].multiselect(
        "Filter prompts",
        sorted({result["prompt_label"] for result in results}),
        key="results_prompt_filter",
    )
    model_filter = filter_columns[1].multiselect(
        "Filter models",
        sorted({result["model"] for result in results}),
        key="results_model_filter",
    )
    outcome_filter = filter_columns[2].multiselect(
        "Filter outcomes",
        ["Pass", "Fail", "Not graded", "Error"],
        key="results_outcome_filter",
    )
    table_rows = [
        row
        for row in all_table_rows
        if (not prompt_filter or row["Prompt"] in prompt_filter)
        and (not model_filter or row["Model"] in model_filter)
        and (not outcome_filter or row["Outcome"] in outcome_filter)
    ]
    if not table_rows:
        st.info("No rows match the current filters.")
        return

    result_frame = pd.DataFrame(table_rows)
    st.dataframe(
        result_frame.style.apply(
            lambda row: [OUTCOME_STYLES.get(row.get("Outcome", ""), "")] * len(row),
            axis=1,
        ),
        hide_index=True,
        width="stretch",
        column_order=RESULT_COLUMN_ORDER,
        column_config={
            "Answer": st.column_config.TextColumn(width="large"),
            "Input": st.column_config.TextColumn(width="large"),
            "Gold label": st.column_config.TextColumn(width="medium"),
            "Score": st.column_config.NumberColumn(format="%.2f"),
            "Latency (s)": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    st.success("Ready to keep this project? Choose **5. Export** in the sidebar.")


def render_evaluate_step():
    """Render editable scenario evaluation in single-model or matrix mode."""
    ensure_model_defaults(st.session_state.app_config)
    profiles = normalize_prompt_profiles(
        st.session_state.app_config.get("prompt_profiles")
    )
    st.session_state.app_config["prompt_profiles"] = profiles
    model_config = st.session_state.app_config["model"]
    labels = {profile["key"]: profile["label"] for profile in profiles}
    db_path, chroma_path = session_paths()

    st.header("Evaluate your assistant")
    st.write(
        "Build a gold-labeled scenario set, then test one model or run every "
        "selected prompt and model as a matrix. Results stay in this browser "
        "session and are not included in an exported app."
    )
    st.warning(
        "Each combination makes a real model call using credentials configured for this scaffolder. "
        "Your provider may charge for these calls."
    )

    provider, provider_ready, untrusted_custom_endpoint = _render_provider_controls(
        model_config
    )
    configured_models = parse_model_names(
        [
            model_config.get("name", ""),
            *parse_model_names(model_config.get("allowed_models", "")),
        ]
    )
    configured_for_provider = (
        configured_models if provider == model_config.get("provider") else []
    )
    if untrusted_custom_endpoint:
        model_choices = []
    else:
        model_choices, choice_source, discovery = get_endpoint_model_choices(
            provider, configured_for_provider
        )
        render_endpoint_model_status(discovery, choice_source)
    allowed_models = host_runnable_models(model_choices)
    configured_model = str(model_config.get("name", "")).strip()
    default_model = (
        configured_model
        if configured_model in model_choices or not model_choices
        else model_choices[0]
    )

    st.subheader("1. Gold-labeled scenarios")
    st.caption(
        "Edit cells directly or upload a UTF-8 CSV. AskLit accepts input/question/query "
        "and gold_label/expected/reference_answer aliases, plus Promptfoo-style "
        "__expected, __description, and __metadata:* columns."
    )
    shared_rubrics = _render_shared_rubrics()
    scenarios = _render_scenario_editor()
    _render_scenario_generator(
        profiles, labels, provider, default_model, allowed_models, provider_ready
    )

    st.subheader("2. Run settings")
    prompt_keys, knowledgebase_keys, model_names = _render_run_settings(
        profiles, labels, model_choices, default_model
    )
    _warn_about_unindexed_knowledgebases(profiles, knowledgebase_keys, db_path)

    matrix = build_evaluation_matrix(
        profiles, prompt_keys, knowledgebase_keys, model_names, scenarios
    )
    judge_run_count = count_judge_calls(matrix, shared_rubrics)
    judge_model = ""
    if judge_run_count:
        st.info(
            "Model-graded rubrics are enabled. The judge applies "
            f"{len(shared_rubrics)} shared rule(s) plus any row-level llm-rubric "
            f"labels, and sees the retrieved passages. A {RUBRIC_PASS_THRESHOLD:.2f} "
            "or higher score passes. A row with both a gold label and shared rules "
            "must satisfy both."
        )
        if model_choices:
            judge_model = st.selectbox(
                "Judge model",
                model_choices,
                index=model_choices.index(default_model),
                key="evaluation_judge_model",
                help=(
                    "The judge is called separately for each rubric-enabled scenario. "
                    "Choose a capable model, and remember that judge calls add cost."
                ),
            )
        else:
            judge_model = st.text_input(
                "Judge model",
                value=default_model,
                key="evaluation_judge_model_text",
                help=(
                    "The judge is called separately for each rubric-enabled scenario. "
                    "Judge calls add cost."
                ),
            )

    top_k = st.slider(
        "Retrieved passages per run", 1, 10, 5, key="evaluation_top_k"
    )
    run_count = len(matrix)
    if run_count:
        call_summary = f"{run_count} answer model call{'s' if run_count != 1 else ''}"
        if judge_run_count:
            call_summary += (
                f" + {judge_run_count} judge call{'s' if judge_run_count != 1 else ''}"
            )
        st.caption(call_summary + " will run.")
    if run_count > MAX_MATRIX_CALLS:
        st.error(f"Reduce the matrix to {MAX_MATRIX_CALLS} model calls or fewer.")

    unapproved = [
        model
        for model in parse_model_names(model_names)
        if get_allowed_models() and model not in allowed_models
    ]
    if unapproved:
        st.error(
            "This scaffolder's administrator has not approved: "
            + ", ".join(f"`{model}`" for model in unapproved)
            + "."
        )

    can_run = bool(
        matrix
        and run_count <= MAX_MATRIX_CALLS
        and provider_ready
        and not unapproved
        and (not judge_run_count or judge_model)
    )
    if st.button("Run evaluation", type="primary", disabled=not can_run):
        progress = st.progress(0, text="Starting experiment…")

        def report(index, total, prompt_label, model):
            progress.progress(
                min(index / total, 1.0) if total else 1.0,
                text=(
                    f"Scenario {min(index + 1, total)}/{total}: {prompt_label} × {model}"
                    if prompt_label
                    else "Experiment complete"
                ),
            )

        with st.spinner("Running the evaluation…"):
            st.session_state.experiment_results = run_evaluation(
                matrix,
                run_id=str(uuid.uuid4()),
                provider=provider,
                judge_model=judge_model,
                shared_rubrics=shared_rubrics,
                top_k=top_k,
                db_path=db_path,
                chroma_path=chroma_path,
                document_labels=get_document_labels(db_path),
                allowed_models=allowed_models,
                progress=report,
            )
        st.session_state.experiment_run_settings = {
            "provider": provider,
            "top_k": top_k,
            "mode": st.session_state.get("evaluation_mode", "Single model"),
            "judge_model": judge_model,
        }

    _render_results(profiles)
