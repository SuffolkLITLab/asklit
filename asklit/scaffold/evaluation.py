"""Run the scenario × prompt × knowledge base × model evaluation matrix.

Kept free of Streamlit so the grading and accounting rules can be tested
directly; the caller supplies a progress callback and receives plain dicts.
"""

import time

from asklit.experiments import (
    build_experiment_messages,
    build_rubric_judge_messages,
    combine_grades,
    deterministic_grade,
    grader_label,
    parse_rubric_grade,
    resolve_rubric_rules,
    response_text,
)
from asklit.llm import call_llm, estimate_tokens
from asklit.observability import log_ai_call_event, safe_error_message
from asklit.rag import query_index

MAX_MATRIX_CALLS = 60
EXPERIMENT_SOURCE = "experiment_lab"
JUDGE_SOURCE = "experiment_lab_judge"


def count_judge_calls(matrix, shared_rubrics):
    """Count the extra judge calls a run will make, for the cost preview."""
    return sum(
        1
        for combination in matrix
        if resolve_rubric_rules(
            shared_rubrics, combination["scenario"].get("__expected", "")
        )
    )


def summarize_by_prompt_and_model(results):
    """Aggregate pass rate for each prompt × model cell."""
    grouped = {}
    for result in results:
        key = (result["prompt_label"], result["model"])
        bucket = grouped.setdefault(key, {"runs": 0, "graded": 0, "passed": 0})
        bucket["runs"] += 1
        if result["passed"] is not None:
            bucket["graded"] += 1
        if result["passed"] is True:
            bucket["passed"] += 1
    return [
        {
            "Prompt": prompt_label,
            "Model": model,
            "Runs": bucket["runs"],
            "Graded": bucket["graded"],
            "Passed": bucket["passed"],
            "Pass rate": (
                bucket["passed"] / bucket["graded"] if bucket["graded"] else None
            ),
        }
        for (prompt_label, model), bucket in sorted(grouped.items())
    ]


def best_configuration(summary_rows):
    """Return the highest-scoring prompt × model cell, if any row was graded."""
    graded = [row for row in summary_rows if row["Pass rate"] is not None]
    if not graded:
        return None
    return max(graded, key=lambda row: (row["Pass rate"], row["Graded"]))


def _judge_answer(
    *,
    question,
    answer,
    rubric_rules,
    context_chunks,
    judge_model,
    provider,
    allowed_models,
    run_id,
    prompt_key,
    knowledgebase,
    call_model,
):
    """Grade one answer with the judge model and account for its own cost."""
    judge_messages = build_rubric_judge_messages(
        question,
        answer,
        "\n".join(f"- {rule}" for rule in rubric_rules),
        context_chunks=context_chunks,
    )
    started = time.perf_counter()
    tokens_in = sum(
        estimate_tokens(message["content"]) for message in judge_messages
    )
    try:
        judge_response = call_model(
            judge_messages,
            stream=False,
            max_tokens_override=300,
            model_override=judge_model,
            provider_override=provider,
            extra_allowed_models=allowed_models,
        )
        judge_text = response_text(judge_response)
        grade = parse_rubric_grade(judge_text)
        error = None
    except Exception as exc:
        judge_text = ""
        grade = None
        error = exc

    elapsed = time.perf_counter() - started
    # The judge is a billed model call of its own, so it is logged and counted
    # separately instead of hiding inside the answer call's totals.
    log_ai_call_event(
        run_id=run_id,
        source=JUDGE_SOURCE,
        provider=provider,
        model=judge_model,
        prompt_key=prompt_key,
        knowledgebase=knowledgebase,
        status="failed" if error else "succeeded",
        stage="judge",
        error=error,
        latency_ms=round(elapsed * 1000),
        tokens_in=tokens_in,
        tokens_out=estimate_tokens(judge_text),
    )
    return {
        "grade": grade,
        "error": error,
        "elapsed": elapsed,
        "tokens": tokens_in + estimate_tokens(judge_text),
    }


def run_evaluation(
    matrix,
    *,
    run_id,
    provider,
    judge_model,
    shared_rubrics,
    top_k,
    db_path,
    chroma_path,
    document_labels,
    allowed_models=None,
    progress=None,
    call_model=call_llm,
    retrieve=query_index,
):
    """Execute every combination and return one result row per combination."""
    results = []
    total = len(matrix)
    for index, combination in enumerate(matrix):
        prompt_profile = combination["prompt"]
        knowledgebase_profile = combination["knowledgebase"]
        model = combination["model"]
        scenario = combination["scenario"]
        question = scenario["input"]
        expected = scenario.get("__expected", "")
        if progress:
            progress(index, total, prompt_profile["label"], model)

        started = time.perf_counter()
        context_chunks = []
        answer = ""
        error = None
        failure_stage = None
        try:
            context_chunks = retrieve(
                question,
                n_results=top_k,
                knowledgebase=knowledgebase_profile["knowledgebase"],
                connected_files=knowledgebase_profile.get("connected_files"),
                db_path=db_path,
                chroma_path=chroma_path,
            )
        except Exception as exc:
            error = exc
            failure_stage = "retrieval"

        if error is None:
            try:
                response = call_model(
                    build_experiment_messages(
                        prompt_profile["prompt"], question, context_chunks
                    ),
                    stream=False,
                    model_override=model,
                    provider_override=provider,
                    extra_allowed_models=allowed_models,
                )
                answer = response_text(response)
                if not answer:
                    error = RuntimeError("The model returned an empty response.")
                    failure_stage = "response"
            except Exception as exc:
                error = exc
                failure_stage = "completion"

        elapsed = time.perf_counter() - started
        answer_tokens_in = (
            estimate_tokens(question)
            + estimate_tokens(prompt_profile["prompt"])
            + sum(
                estimate_tokens(chunk.get("content", "")) for chunk in context_chunks
            )
        )
        safe_error = log_ai_call_event(
            run_id=run_id,
            source=EXPERIMENT_SOURCE,
            provider=provider,
            model=model,
            prompt_key=prompt_profile["key"],
            knowledgebase=knowledgebase_profile["knowledgebase"],
            status="failed" if error else "succeeded",
            stage=failure_stage or "completion",
            error=error,
            latency_ms=round(elapsed * 1000),
            tokens_in=answer_tokens_in,
            tokens_out=estimate_tokens(answer),
        )

        rubric_rules = resolve_rubric_rules(shared_rubrics, expected)
        row_grade = None if error else deterministic_grade(answer, expected)
        rubric_grade = None
        judge_elapsed = 0.0
        judge_tokens = 0
        if error is None and rubric_rules:
            judged = _judge_answer(
                question=question,
                answer=answer,
                rubric_rules=rubric_rules,
                context_chunks=context_chunks,
                judge_model=judge_model,
                provider=provider,
                allowed_models=allowed_models,
                run_id=run_id,
                prompt_key=prompt_profile["key"],
                knowledgebase=knowledgebase_profile["knowledgebase"],
                call_model=call_model,
            )
            judge_elapsed = judged["elapsed"]
            judge_tokens = judged["tokens"]
            if judged["error"] is not None:
                error = judged["error"]
                safe_error = safe_error_message(judged["error"])
                failure_stage = "judge"
                rubric_grade = {
                    "passed": None,
                    "score": None,
                    "reason": "Model rubric judge failed",
                }
            else:
                rubric_grade = judged["grade"]

        if error is not None and failure_stage != "judge":
            grade = {"passed": None, "score": None, "reason": "Model call failed"}
        else:
            grade = combine_grades(row_grade, rubric_grade)

        results.append(
            {
                "run_id": run_id,
                "prompt_label": prompt_profile["label"],
                "prompt_key": prompt_profile["key"],
                "knowledgebase_label": knowledgebase_profile["label"],
                "knowledgebase": knowledgebase_profile["knowledgebase"],
                "model": model,
                "provider": provider,
                "scenario": scenario.get("__description") or question,
                "input": question,
                "expected": expected,
                "answer": answer,
                "grader": grader_label(row_grade, rubric_grade),
                "judge_model": judge_model if rubric_rules else "",
                "passed": grade["passed"],
                "score": grade["score"],
                "grade_reason": grade["reason"],
                "error": safe_error,
                "failure_stage": failure_stage,
                "elapsed": elapsed,
                "judge_elapsed": judge_elapsed,
                "tokens": answer_tokens_in + estimate_tokens(answer),
                "judge_tokens": judge_tokens,
                "sources": [
                    {
                        "filename": document_labels.get(
                            chunk.get("metadata", {}).get("document_id"),
                            "Knowledge base document",
                        ),
                        "page": chunk.get("metadata", {}).get("page_number"),
                        "content": chunk.get("content", ""),
                    }
                    for chunk in context_chunks
                ],
            }
        )
    if progress:
        progress(total, total, "", "")
    return results
