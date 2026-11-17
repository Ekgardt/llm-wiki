"""Validating a validated compile operation gives the same operation (audit 2026-09-27 A-2).

docs/research/2026-09-27-a-derived-field-is-derived-again.md
"""
from __future__ import annotations

import hashlib

import compile_receipts
from llm_wiki import compile_pipeline_prompts, compile_pipeline_receipts, compile_pipeline_sources

LOGICAL_PATH = "knowledge/daily/2026-09-20.md"
DAY = (
    b"# Daily Log: 2026-09-20\n\n## [10:00:00] session-end | abc\n\n- Trigger: `session-end`\n"
    b"- Agent: `claude`\n- Project slug: `llm-wiki`\n- Capture intent: `x`\n- Tier: `major`\n\n"
    b"## Decisions made\n- Always run the privacy guard before pushing to the public repository.\n"
)
OPERATION = {
    "action": "create",
    "category": "patterns",
    "slug": "privacy-guard-before-push",
    "title": "Privacy guard before push",
    "summary": "Always run the privacy guard before pushing to the public repository.",
    "body_markdown": "Run the guard.",
    "body_section": "Lesson",
    "related": [],
    "evidence": [
        {
            "daily_date": "2026-09-20",
            "timestamp": "10:00:00",
            "quoted_text": "Always run the privacy guard before pushing to the public repository.",
            "claim": "guard before push",
        }
    ],
}


def _inputs() -> compile_pipeline_sources.CompileInputs:
    digest = hashlib.sha256(DAY).hexdigest()
    daily = compile_receipts.DailySnapshot(LOGICAL_PATH, DAY, digest, 0, 1, 0, len(DAY))
    return compile_pipeline_sources.CompileInputs((daily,), (compile_receipts.SourceSnapshot(LOGICAL_PATH, DAY, digest),), ())


def test_validating_twice_gives_the_same_operation() -> None:
    inputs = _inputs()
    once, _ = compile_pipeline_prompts.validate_semantic_operation(dict(OPERATION), inputs)
    twice, _ = compile_pipeline_prompts.validate_semantic_operation(dict(once), inputs)

    assert (once.get("project"), twice) == ("llm-wiki", once)


def test_a_plan_whose_evidence_names_a_project_validates_and_materializes() -> None:
    inputs = _inputs()
    plan = compile_pipeline_prompts.normalize_plan([dict(OPERATION)], inputs)
    compile_pipeline_prompts.validate_compile_plan(plan, inputs)
    planned = compile_pipeline_prompts.planned_operation(dict(OPERATION), inputs)

    materialized = compile_pipeline_receipts.materialized_operations([planned], inputs, "2026-09-20T10:00:00Z")

    assert materialized[0][0]["path"] == "knowledge/notes/privacy-guard-before-push.md"


def test_the_model_cannot_supply_a_derived_field() -> None:
    validated, _ = compile_pipeline_prompts.validate_semantic_operation({**OPERATION, "project": "someone-else"}, _inputs())

    assert validated["project"] == "llm-wiki"
