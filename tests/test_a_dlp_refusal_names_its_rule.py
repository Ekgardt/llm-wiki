"""A blocked model output names which rule fired, never what it matched.

On the live vault 72 session captures were refused `dlp_content_blocked` on every
retry from 2026-09-26 to 2026-09-28, and the refusal said only "model output
contains protected content": nothing told a real secret from a false positive.
"""

from __future__ import annotations

import pytest
from model_dlp import DLPContentBlocked, DLPPolicy, require_safe_model_output

# Built at run time so no token-shaped literal sits in the repository.
_TOKEN = "ghp_" + "a" * 36


def _refusal(text: str) -> str:
    with pytest.raises(DLPContentBlocked) as refused:
        require_safe_model_output(text, DLPPolicy())
    return str(refused.value)


def test_the_refusal_names_the_rule_and_its_count_and_not_the_value() -> None:
    said = _refusal(f"the page cites {_TOKEN} twice: {_TOKEN}")

    assert (said, _TOKEN in said) == (
        "model output contains protected content (REDACTED_GITHUB_TOKEN x2)",
        False,
    )


def test_a_marker_already_in_the_output_is_not_counted_as_a_finding() -> None:
    said = _refusal(f"an earlier [REDACTED] line, then {_TOKEN}")

    assert said == "model output contains protected content (REDACTED_GITHUB_TOKEN x1)"
