"""Validating a validated compile operation gives the same operation (audit 2026-09-27 A-2).

docs/research/2026-09-27-a-derived-field-is-derived-again.md
"""
from __future__ import annotations

import hashlib

import compile_memory
import pytest

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


def _inputs() -> compile_memory.CompileInputs:
    digest = hashlib.sha256(DAY).hexdigest()
    daily = compile_memory.DailySnapshot(LOGICAL_PATH, DAY, digest, 0, 1, 0, len(DAY))
    return compile_memory.CompileInputs((daily,), (compile_memory.SourceSnapshot(LOGICAL_PATH, DAY, digest),), ())


def test_validating_twice_gives_the_same_operation() -> None:
    inputs = _inputs()
    once, _ = compile_memory._validate_semantic_operation(dict(OPERATION), inputs)
    twice, _ = compile_memory._validate_semantic_operation(dict(once), inputs)

    assert (once.get("project"), twice) == ("llm-wiki", once)


def test_a_plan_whose_evidence_names_a_project_validates_and_materializes() -> None:
    inputs = _inputs()
    plan = compile_memory._normalize_plan([dict(OPERATION)], inputs)
    compile_memory.validate_compile_plan(plan, inputs)
    planned = compile_memory._planned_operation(dict(OPERATION), inputs)

    materialized = compile_memory._materialized_operations([planned], inputs, "2026-09-20T10:00:00Z")

    assert materialized[0][0]["path"] == "knowledge/notes/privacy-guard-before-push.md"


def test_the_model_cannot_supply_a_derived_field() -> None:
    validated, _ = compile_memory._validate_semantic_operation({**OPERATION, "project": "someone-else"}, _inputs())

    assert validated["project"] == "llm-wiki"


@pytest.mark.parametrize("action", ["create", "update"])
@pytest.mark.parametrize("field", ["title", "summary", "body_markdown"])
def test_model_prose_cannot_publish_an_unbound_daily_reference(action: str, field: str) -> None:
    operation = {**OPERATION, "action": action, field: "See `daily:2026-09-20 10:00:00`."}

    with pytest.raises(ValueError, match="evidence reference is not canonical"):
        compile_memory._validate_semantic_operation(operation, _inputs())


def test_evidence_description_cannot_smuggle_a_malformed_reference() -> None:
    evidence = [{**OPERATION["evidence"][0], "claim": "See `daily:2026-09-20 10:00:00`."}]

    with pytest.raises(ValueError, match="evidence reference is not canonical"):
        compile_memory._validate_semantic_operation({**OPERATION, "evidence": evidence}, _inputs())


def test_a_canonical_prose_reference_must_resolve_in_the_compile_snapshot() -> None:
    _operation, bindings = compile_memory._validate_semantic_operation(dict(OPERATION), _inputs())
    reference = bindings[0]["reference"].replace(hashlib.sha256(DAY).hexdigest(), "0" * 64)

    with pytest.raises(ValueError, match="source is absent from the snapshot"):
        compile_memory._validate_semantic_operation({**OPERATION, "body_markdown": f"See `{reference}`."}, _inputs())


def test_bound_prose_references_survive_revalidation_unchanged() -> None:
    inputs = _inputs()
    _operation, bindings = compile_memory._validate_semantic_operation(dict(OPERATION), inputs)
    body = f"See `{bindings[0]['reference']}`."
    operation = {**OPERATION, "body_markdown": body}

    once, _ = compile_memory._validate_semantic_operation(operation, inputs)
    twice, _ = compile_memory._validate_semantic_operation(once, inputs)

    assert once["body_markdown"] == body
    assert twice == once
