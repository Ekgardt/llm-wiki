"""Original permanent representation assertions, without publishing IO."""

import pytest
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES
from llm_wiki import breadcrumb_documents as evidence

from tests.modules.breadcrumb_documents.contract_helpers import _source


@pytest.mark.parametrize("content", [
    b"a complete prompt", ('```\n---\n# quoted heading\n"\\\u2028\u65e5' * 1000).encode(),
    b"x" * (2 * 1_048_576),
], ids=["small", "quoted-unicode", "ascii-2-mib"])
def test_permanent_markdown_reconstructs_the_complete_source_without_runtime(content):
    head, documents = _source(content)

    assert evidence.restore_source(head, documents.__getitem__) == content
    assert next(reversed(documents)) == head
    assert all(len(document) <= MAX_KNOWLEDGE_PAGE_BYTES for document in documents.values())
    assert all(document.startswith(b"---\ntype: raw-source\n") for document in documents.values())


def test_a_missing_permanent_part_cannot_prove_completion():
    head, documents = _source(b"x" * (2 * 1_048_576))
    del documents[next(iter(documents))]

    with pytest.raises(KeyError):
        evidence.restore_source(head, documents.__getitem__)


@pytest.mark.parametrize("target", ["head", "part"])
def test_editing_a_permanent_record_invalidates_the_proof(target):
    head, documents = _source(b"complete prompt")
    path = head if target == "head" else next(iter(documents))
    documents[path] += b"unverified change\n"

    with pytest.raises((ValueError, TypeError)):
        evidence.restore_source(head, documents.__getitem__)


def test_a_complete_source_moved_to_another_day_does_not_prove_the_original_path():
    head, documents = _source(b"complete prompt")
    changed = head.replace("2026-09-29", "2026-09-30")
    documents[changed] = documents.pop(head)

    with pytest.raises(ValueError, match="conflicts"):
        evidence.restore_source(changed, documents.__getitem__)
