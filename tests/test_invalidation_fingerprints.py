"""The five invalidation fingerprints are one value under five names.

`evidence_graph_builder._semantic_changes` -- the only reader that does anything
with them -- asks whether a changed source's fingerprints moved, and uses the
answer to seed the dependent closure and to arm workspace invalidation.
`doctor._source_extraction` answers by hashing the content, so today every
byte-level change is a "semantic" change and the five keys discriminate nothing.

That is deliberate, and this test exists so the next reader does not mistake it
for an accident of five suggestive key names. Content-derived fingerprints
over-invalidate; they can never under-invalidate, so the conservative direction
is the safe one.

Only `exports` could be given a real definition. A source is legible to another
source solely through the definitions it contributes to `code_extractor`'s
shared `definitions` / `python_scopes` / `modules` indexes -- `imports` and
`aliases` are a per-source local table that never enters a shared index,
`signatures` already live inside the export identity key, and a project journal
is extracted alone with no shared universe at all.

Making `exports` real was built and measured, and it does not pay: it collapses
a code edit's rebuild set by two orders of magnitude and leaves the delta pass
no faster, because `doctor._SourceExtractionAdapter._code_result` batches
`extract_code` over *every* code source the moment one is rebuilt, and the
edited source is always in the rebuild set. It also carries a latent
correctness hole around ORM table nodes.

**If you make these keys real, delete this test and put the new measurement in
its place.** The numbers, the sources, and the argument are in
`docs/research/2026-08-29-what-an-invalidation-fingerprint-can-mean.md`.
"""

from __future__ import annotations

import hashlib

import doctor
from evidence_graph_builder import SourceExtraction

_KEYS = ("exports", "imports", "signatures", "aliases", "project_metadata")


class _EmptyResult:
    """The record collections `_source_extraction` copies out of an extraction."""

    nodes = ()
    occurrences = ()
    assertions = ()
    evidence = ()
    observations = ()
    dependencies = ()
    source_dependencies = ()
    workspace_sensitive = False


def _fingerprints(content: bytes) -> dict[str, str]:
    extraction = doctor._source_extraction(SourceExtraction, _EmptyResult(), content)
    return dict(extraction.invalidation_fingerprints)


def _expected(content: bytes) -> dict[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        key: hashlib.sha256(f"{key}:{digest}".encode("ascii")).hexdigest()
        for key in _KEYS
    }


def test_every_fingerprint_is_a_function_of_the_content_digest() -> None:
    content = b"def f(a):\n    return a\n"
    assert _fingerprints(content) == _expected(content)


def test_a_comment_only_edit_still_moves_every_fingerprint() -> None:
    """The property that makes the machinery inert: no early cutoff exists.

    A trailing comment changes no definition, so no dependent can observe it,
    yet all five fingerprints move and `_semantic_changes` reports a semantic
    change. This is the conservative answer, not a wrong one.
    """
    before = _fingerprints(b"def f(a):\n    return a\n")
    after = _fingerprints(b"def f(a):\n    return a\n# invisible to a dependent\n")
    assert before != after
    assert all(before[key] != after[key] for key in _KEYS)


def test_the_five_keys_are_the_ones_the_builder_requires() -> None:
    """`_require_invalidation_fingerprints` rejects any other key set."""
    from evidence_graph_builder import _INVALIDATION_KEYS

    assert set(_KEYS) == set(_INVALIDATION_KEYS)


def test_each_fingerprint_is_a_lowercase_sha256_digest() -> None:
    """The builder validates the shape, so the producer must honour it."""
    values = _fingerprints(b"x = 1\n").values()
    assert all(len(value) == 64 for value in values)
    assert all(set(value) <= set("0123456789abcdef") for value in values)
