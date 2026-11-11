"""Shared evaluation planning, CSV adapters, and bounded scenario generation."""

from itertools import product

from fastapi import HTTPException
from asklit.experiments import resolve_rubric_rules, parse_generated_scenarios
from .schemas import EvaluationOptions, Scenario


def scenario_from_scaffold(row):
    return Scenario(
        input=row["input"],
        expected=row.get("__expected", ""),
        description=row.get("__description", ""),
        metadata={k: v for k, v in row.items() if k.startswith("__metadata:")},
    ).model_dump()


def scaffold_scenarios(rows):
    return [
        {
            "input": r["input"],
            "__expected": r["expected"],
            "__description": r.get("description", ""),
            **r.get("metadata", {}),
        }
        for r in rows
    ]


def plan(config, settings):
    options = EvaluationOptions.model_validate(
        config.get("evaluation", {})
    ).model_dump()
    profiles = {p["key"]: p for p in config["profiles"]}
    first = next(iter(profiles))
    prompts = list(dict.fromkeys(options["prompts"] or [first]))
    sources = list(dict.fromkeys(options["sources"] or [first]))
    models = list(dict.fromkeys(options["models"] or [config["model_preset"]]))
    judge = options["judge_model"] or config["model_preset"]
    if any(k not in profiles for k in prompts + sources):
        raise HTTPException(
            422, "Select an existing prompt and source group for evaluation."
        )
    if any(m not in settings.models for m in models + [judge]):
        raise HTTPException(
            422, "Select operator-configured evaluation and judge models."
        )
    if options["mode"] == "single" and any(
        len(v) != 1 for v in [prompts, sources, models]
    ):
        raise HTTPException(
            422, "Single mode requires one prompt, source group and model."
        )
    combinations = len(prompts) * len(sources) * len(models)
    answers = combinations * len(config["scenarios"])
    judges = combinations * sum(
        bool(resolve_rubric_rules(config["rubrics"], s["expected"]))
        for s in config["scenarios"]
    )
    return {
        "prompts": prompts,
        "sources": sources,
        "models": models,
        "judge_model": judge,
        "top_k": options["top_k"],
        "answers": answers,
        "judges": judges,
        "total_calls": answers + judges,
        "limit": settings.evaluation_max_calls,
    }


def combinations(config, run):
    profiles = {p["key"]: p for p in config["profiles"]}
    for scenario, prompt, source, model in product(
        config["scenarios"], run["prompts"], run["sources"], run["models"]
    ):
        yield scenario, profiles[prompt], profiles[source], model


def generate(runtime, docs, profile, model, count):
    relevant = [
        d
        for d in docs
        if d.knowledgebase == profile["knowledgebase"]
        and (not profile["connected_files"] or d.filename in profile["connected_files"])
    ]
    sample = "\n\n".join(c["content"] for d in relevant for c in d.chunks[:30])[:12000]
    if not sample:
        raise HTTPException(
            422, "Add and index sources for this prompt before generating questions."
        )
    messages = [
        {
            "role": "system",
            "content": "Create concise evaluation datasets. Return only a JSON array of objects with input, __expected and __description. Write realistic questions answerable from the supplied material. Use icontains: followed by the shortest decisive phrase as the expected answer. Do not invent facts.",
        },
        {
            "role": "user",
            "content": f"Create {count} diverse scenarios.\nSYSTEM PROMPT:\n{profile['prompt']}\nSOURCE MATERIAL:\n{sample}",
        },
    ]
    rows = parse_generated_scenarios(runtime.call(messages, model))
    if not rows:
        raise ValueError("No generated questions")
    return [scenario_from_scaffold(row) for row in rows[:count]]
