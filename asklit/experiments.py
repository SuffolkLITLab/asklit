import csv
import io
import json
import re
from itertools import product

MAX_CONTEXT_CHARS = 8000
QUESTION_COLUMN_NAMES = ("input", "question", "query", "prompt", "text")
EXPECTED_COLUMN_NAMES = (
    "__expected",
    "gold_label",
    "goldlabel",
    "expected",
    "expected_answer",
    "expectedanswer",
    "reference_answer",
    "referenceanswer",
)


def build_experiment_messages(system_prompt, user_query, context_chunks):
    """Build a one-turn RAG request from an in-progress scaffold prompt."""
    context_parts = []
    current_length = 0
    for index, chunk in enumerate(context_chunks):
        content = str(chunk.get("content", "")).strip()
        if len(content) < 80:
            continue
        if current_length + len(content) > MAX_CONTEXT_CHARS:
            break
        context_parts.append(f"--- SOURCE {index + 1} ---\n{content}")
        current_length += len(content)

    context = "\n\n".join(context_parts)
    full_system_prompt = (
        f"{system_prompt}\n\n"
        f"RELEVANT CONTEXT FROM THE KNOWLEDGE BASE:\n{context}\n\n"
        "INSTRUCTIONS FOR USING CONTEXT:\n"
        "1. When context is provided and it is relevant, ground the answer in that context before adding general background.\n"
        "2. If the context only partially answers the question, say what the context supports and then add any clearly labeled general guidance.\n"
        "3. If the context does not contain the answer, or if the user is asking a general question, use your general knowledge to provide a helpful response."
    )
    return [
        {"role": "system", "content": full_system_prompt},
        {"role": "user", "content": user_query},
    ]


def parse_model_names(value):
    """Normalize comma- or newline-separated model names without duplicates."""
    if isinstance(value, str):
        candidates = value.replace("\n", ",").split(",")
    else:
        candidates = value or []

    models = []
    seen = set()
    for candidate in candidates:
        model = str(candidate).strip()
        if model and model not in seen:
            models.append(model)
            seen.add(model)
    return models


def build_experiment_matrix(prompt_profiles, prompt_keys, knowledgebase_keys, models):
    """Return the requested prompt × knowledge base × model combinations."""
    profiles_by_key = {profile["key"]: profile for profile in prompt_profiles}
    prompts = [profiles_by_key[key] for key in prompt_keys if key in profiles_by_key]
    knowledgebases = [
        profiles_by_key[key] for key in knowledgebase_keys if key in profiles_by_key
    ]
    return [
        {
            "prompt": prompt_profile,
            "knowledgebase": knowledgebase_profile,
            "model": model,
        }
        for prompt_profile, knowledgebase_profile, model in product(
            prompts, knowledgebases, parse_model_names(models)
        )
    ]


def normalize_scenario_rows(rows):
    """Normalize editable or uploaded Promptfoo-like rows for AskLit."""
    normalized = []
    for index, original in enumerate(rows or []):
        row = {str(key).strip(): value for key, value in dict(original).items()}
        lower_keys = {key.casefold(): key for key in row}

        input_key = next(
            (lower_keys[name] for name in QUESTION_COLUMN_NAMES if name in lower_keys),
            None,
        )
        if input_key is None:
            input_key = next(
                (key for key in row if key and not key.startswith("__")), None
            )

        expected_key = next(
            (lower_keys[name] for name in EXPECTED_COLUMN_NAMES if name in lower_keys),
            None,
        )
        description_key = lower_keys.get("__description") or lower_keys.get(
            "description"
        )
        question = str(row.get(input_key, "") or "").strip()
        if not question:
            continue

        scenario = {
            "input": question,
            "__expected": str(row.get(expected_key, "") or "").strip(),
            "__description": str(
                row.get(description_key, "") or f"Scenario {index + 1}"
            ).strip(),
        }
        for key, value in row.items():
            if key.startswith("__metadata:"):
                scenario[key] = str(value or "").strip()
        normalized.append(scenario)
    return normalized


def parse_scenario_csv(value):
    """Read UTF-8 CSV data and return normalized Promptfoo-like scenarios."""
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, bytes):
        value = value.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(str(value or "")))
    if not reader.fieldnames:
        raise ValueError("The CSV file needs a header row.")
    rows = normalize_scenario_rows(reader)
    if not rows:
        raise ValueError(
            "No scenarios were found. Include an input, question, query, prompt, or text column."
        )
    return rows


def scenarios_to_csv(rows):
    """Export scenarios using Promptfoo's special expected/description columns."""
    rows = normalize_scenario_rows(rows)
    metadata_columns = sorted(
        {key for row in rows for key in row if key.startswith("__metadata:")}
    )
    output = io.StringIO()
    fieldnames = ["input", "__expected", "__description", *metadata_columns]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def parse_generated_scenarios(text):
    """Extract a JSON scenario array from an LLM response."""
    value = str(text or "").strip()
    fenced = re.search(
        r"```(?:json)?\s*(.*?)```", value, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        value = fenced.group(1).strip()
    if not value.startswith("["):
        start, end = value.find("["), value.rfind("]")
        if start >= 0 and end > start:
            value = value[start : end + 1]
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "The model did not return a valid JSON scenario list."
        ) from exc
    if not isinstance(payload, list):
        raise ValueError("The model response must be a JSON list of scenarios.")
    return normalize_scenario_rows(payload)


def evaluate_expected(output, expected):
    """Evaluate a transparent subset of Promptfoo's CSV assertion syntax."""
    output = str(output or "")
    expected = str(expected or "").strip()
    if not expected:
        return {"passed": None, "score": None, "reason": "No gold label"}

    assertion_type = "equals"
    assertion_value = expected
    match = re.match(r"^([a-z-]+)(?:\([^)]*\))?\s*:\s*(.*)$", expected, re.DOTALL)
    if match:
        assertion_type, assertion_value = match.group(1).lower(), match.group(2)

    if assertion_type in {"llm-rubric", "model-rubric", "rubric"}:
        return {
            "passed": None,
            "score": None,
            "reason": "Model rubric (judge required)",
            "rubric": assertion_value.strip(),
        }

    normalized_output = " ".join(output.split())
    normalized_value = " ".join(assertion_value.split())
    if assertion_type == "equals":
        passed = normalized_output == normalized_value
    elif assertion_type == "contains":
        passed = normalized_value in normalized_output
    elif assertion_type == "icontains":
        passed = normalized_value.casefold() in normalized_output.casefold()
    elif assertion_type in {"contains-any", "icontains-any"}:
        candidates = [
            item.strip() for item in assertion_value.split(",") if item.strip()
        ]
        if assertion_type.startswith("i"):
            passed = any(
                item.casefold() in normalized_output.casefold() for item in candidates
            )
        else:
            passed = any(item in normalized_output for item in candidates)
    elif assertion_type in {"contains-all", "icontains-all"}:
        candidates = [
            item.strip() for item in assertion_value.split(",") if item.strip()
        ]
        if assertion_type.startswith("i"):
            passed = all(
                item.casefold() in normalized_output.casefold() for item in candidates
            )
        else:
            passed = all(item in normalized_output for item in candidates)
    else:
        return {
            "passed": None,
            "score": None,
            "reason": f"Unsupported assertion: {assertion_type}",
        }
    return {
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "reason": assertion_type,
    }


def is_model_rubric(expected):
    """Return whether a gold label delegates grading to an LLM judge."""
    return bool(
        re.match(
            r"^(?:llm-rubric|model-rubric|rubric)\s*:\s*.+$",
            str(expected or "").strip(),
            flags=re.IGNORECASE | re.DOTALL,
        )
    )


def rubric_text(expected):
    """Extract the human-readable rubric from an LLM assertion."""
    match = re.match(
        r"^(?:llm-rubric|model-rubric|rubric)\s*:\s*(.+)$",
        str(expected or "").strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def build_rubric_judge_messages(question, answer, rubric):
    """Build a constrained JSON request for a model-graded rubric."""
    return [
        {
            "role": "system",
            "content": (
                "You are an evaluation judge. Grade the answer against the rubric. "
                "Return only a JSON object with numeric score from 0 to 1 and a "
                "short narrative explanation in the reason field. Pass means the "
                "answer substantially satisfies all listed rubric rules; do not "
                "require exact wording. The application determines pass/fail from "
                "the score, so do not omit score."
            ),
        },
        {
            "role": "user",
            "content": (
                f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n\n"
                f"RUBRIC:\n{rubric}"
            ),
        },
    ]


def parse_rubric_grade(text, pass_threshold=0.7):
    """Parse a judge response and normalize it to AskLit's grade shape."""
    value = str(text or "").strip()
    fenced = re.search(
        r"```(?:json)?\s*(.*?)```", value, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        value = fenced.group(1).strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("The rubric judge did not return valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The rubric judge response must be a JSON object.")
    try:
        score = max(0.0, min(1.0, float(payload["score"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The rubric judge response needs a numeric score.") from exc
    passed = score >= pass_threshold
    reason = str(
        payload.get("reason")
        or payload.get("rationale")
        or payload.get("narrative")
        or payload.get("explanation")
        or "Model rubric"
    ).strip()
    return {
        "passed": passed,
        "score": score,
        "reason": f"Model rubric: {reason}",
    }


def build_evaluation_matrix(
    prompt_profiles, prompt_keys, knowledgebase_keys, models, scenarios
):
    """Return scenario × prompt × knowledge base × model combinations."""
    combinations = build_experiment_matrix(
        prompt_profiles, prompt_keys, knowledgebase_keys, models
    )
    return [
        {**combination, "scenario": scenario}
        for scenario, combination in product(
            normalize_scenario_rows(scenarios), combinations
        )
    ]


def response_text(response):
    """Extract text from a non-streaming LiteLLM response."""
    choices = getattr(response, "choices", None)
    if not choices and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return ""

    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message", {})
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return str(content or "")
