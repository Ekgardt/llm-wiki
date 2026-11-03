"""Original parameterized retry identity assertions, unchanged."""
from __future__ import annotations

import pytest
import transaction_lineage

from tests.modules.transaction_lineage.fixtures import COMMITTED, DIGEST, REFUSED


@pytest.mark.parametrize(
    ("operation_id", "expected"),
    (
        ("compile:abc", "compile:abc"),
        ("compile:abc#2", "compile:abc"),
        ("post-tool:abc:cas:1", "post-tool:abc"),
        ("post-tool:abc#3:cas:2", "post-tool:abc"),
        ("compile:abc:cas:notanumber", "compile:abc:cas:notanumber"),
    ),
)
def test_the_base_identity_strips_only_retry_ordinals(
    operation_id: str, expected: str
) -> None:
    assert transaction_lineage.base_operation_identity(operation_id) == expected


@pytest.mark.parametrize(
    ("operation_id", "expected"),
    (
        (REFUSED, f"project:llm-wiki:716:{DIGEST}"),
        (COMMITTED, f"project:llm-wiki:716:{DIGEST}"),
        (f"{COMMITTED}:cas:2", f"project:llm-wiki:716:{DIGEST}"),
        # Malformed shapes stay as they are — fail closed, never resolve.
        (f"project:llm-wiki:716:attempt:1:{DIGEST}", None),
        (f"project:llm-wiki:notanumber:attempt:1:epoch:2:{DIGEST}", None),
        ("compile:abc#2", "compile:abc"),
        ("post-tool:abc:cas:1", "post-tool:abc"),
    ),
)
def test_the_base_identity_removes_only_the_attempt_epoch_pair(
    operation_id: str, expected: str | None
) -> None:
    resolved = transaction_lineage.base_operation_identity(operation_id)
    assert resolved == (operation_id if expected is None else expected)
