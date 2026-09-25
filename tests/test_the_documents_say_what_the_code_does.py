"""The public documents describe the code as it is, not as it was.

On 2026-09-24 the three READMEs told a reader to keep the retired legacy index,
three documents said the system makes no automatic Git operation although the
nightly fast-forwards, and two showed a `--slug` flag that does not exist. See
docs/research/2026-09-24-the-documents-say-what-the-code-does.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CURRENT_DOCUMENTS = (
    "README.md",
    "README.ru.md",
    "README.zh-CN.md",
    "docs/USER-GUIDE.md",
    "docs/ARCHITECTURE.md",
    "docs/STRUCTURE.md",
    "docs/operating-model.md",
)
STALE_CLAIMS = (
    re.compile(r"keep legacy `cache/index\.sqlite`", re.IGNORECASE),
    re.compile(r"falls back to the legacy BM25 index", re.IGNORECASE),
    re.compile(r"legacy FTS/vector/Lance paths", re.IGNORECASE),
    re.compile(r"performs no automatic Git operation", re.IGNORECASE),
    re.compile(r"build_context\.py --slug"),
    re.compile(r"removed during implementation"),
)


@pytest.mark.parametrize("document", CURRENT_DOCUMENTS)
def test_no_current_document_repeats_a_retired_claim(document: str) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")

    assert [pattern.pattern for pattern in STALE_CLAIMS if pattern.search(text)] == []


@pytest.mark.parametrize(
    "document",
    ["docs/DEVELOPER-AUDIT-STATUS-2026-08-14.md", "docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md", "docs/ROADMAP-v5.md"],
)
def test_a_superseded_status_document_says_it_is_history(document: str) -> None:
    head = (ROOT / document).read_text(encoding="utf-8")[:600]

    assert "docs/AUDIT-2026-09-24-live.md" in head
