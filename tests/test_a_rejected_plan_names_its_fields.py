"""A compile plan refused for extra fields says which fields they were."""
from __future__ import annotations

import pytest
from llm_wiki import compile_pipeline_operations, compile_pipeline_sources

VALID_FIELDS = dict.fromkeys(compile_pipeline_operations.SEMANTIC_FIELDS, "x")


def _rejection(extra: dict[str, str]) -> str:
    with pytest.raises(ValueError, match="unsupported semantic fields") as caught:
        compile_pipeline_operations.require_semantic_shape({**VALID_FIELDS, **extra})
    return str(caught.value)


def test_the_rejection_names_each_unsupported_field_in_sorted_order() -> None:
    assert _rejection({"zeta": "1", "alpha": "2"}).endswith(": 'alpha', 'zeta'")


def test_every_field_is_named_whole_however_many_share_a_long_prefix() -> None:
    fields = {f"{'shared-prefix-' * 10}{index}": "1" for index in range(8)}

    message = _rejection(fields)

    assert all(repr(name) in message for name in fields)


def test_the_report_of_a_rejection_stays_bounded_however_long_it_is(capsys) -> None:
    message = _rejection({f"{'k' * 500}{index}": "1" for index in range(8)})
    assert len(message) > compile_pipeline_sources.MAX_FAILURE_DETAIL_CHARS

    compile_pipeline_sources.report_stage_detail("draft", "validation_error", message)

    reported = capsys.readouterr().err.strip().removeprefix("compile_memory: draft validation_error: ")
    assert reported == message[: compile_pipeline_sources.MAX_FAILURE_DETAIL_CHARS]
