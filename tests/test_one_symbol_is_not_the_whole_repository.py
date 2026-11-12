"""A question about one name reads only the sources that mention it.

`find_dead_code(symbol=...)` used to parse and walk every Python source in the
repository to answer about one function — 551 files and 11 s of a single answer
on the installed vault, measured 2026-09-12. Narrowing must preserve Python encoding,
normalization and literal concatenation, not just byte substrings. The answer
says how many sources it skipped. Research:
`docs/research/2026-09-12-sixteen-of-sixteen.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import value_references  # noqa: E402

MENTIONS = b"""\
from helpers import wanted


def caller():
    return wanted
"""

SILENT = b"""\
def unrelated():
    return 1
"""


def _sources() -> list[tuple[str, bytes]]:
    return [("pkg/mentions.py", MENTIONS), ("pkg/silent.py", SILENT)]


def test_a_narrowed_index_reads_only_the_sources_that_mention_the_name():
    index = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    assert (index.parsed_sources, index.skipped_sources) == (1, 1)
    assert index.names_a_value("wanted") is True


def test_a_narrowed_index_gives_the_same_answer_as_the_whole_read():
    wide = value_references.build_reference_index(_sources())
    narrow = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    assert narrow.names_a_value("wanted") == wide.names_a_value("wanted")


def test_a_narrowed_index_refuses_a_name_it_never_read_for():
    """Fail closed: answering "nothing names it" from unread sources is a lie."""
    index = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    with pytest.raises(ValueError):
        index.names_a_value("unrelated")


def test_the_report_states_what_the_scan_skipped():
    index = value_references.build_reference_index(
        _sources(), names=frozenset({"wanted"})
    )

    assert index.as_report()["reference_skipped_sources"] == 1


@pytest.mark.parametrize('source,name', [
    (b'lookup="\\u0066oo"', 'foo'),
    (b'lookup="\\x66oo"', 'foo'),
    (b'lookup="\\146oo"', 'foo'),
    (b'lookup="\\N{LATIN SMALL LETTER F}oo"', 'foo'),
    (b'lookup="fo" "o"', 'foo'),
    (b'lookup=("fo" # comment\n "o")', 'foo'),
    (b'lookup=f"\\u0066oo"', 'foo'),
    (b'lookup=f"fo" f"o"', 'foo'),
    (b'lookup=rf"fo" "o"', 'foo'),
    (b'lookup=t"\\u0066oo"', 'foo'),
    (b'lookup=f"{1:\\u0066oo}"', 'foo'),
    (b"lookup=f'{f\"\\u0066oo\"}'", 'foo'),
    (b'# coding: utf-7\nlookup="+AGYAbwBv-"', 'foo'),
    ('lookup=K'.encode(), 'K'),
])
def test_narrowing_preserves_python_semantic_spelling(source, name):
    sources = [('sample.py', source)]
    full = value_references.build_reference_index(sources)
    narrowed = value_references.build_reference_index(sources, names=frozenset({name}))
    assert full.names_a_value(name) is True
    assert narrowed.names_a_value(name) is True


def test_normalized_method_keeps_dispatch_evidence():
    source = 'class X(Base):\n def K(self):pass'.encode()
    index = value_references.build_reference_index([('sample.py', source)], names=frozenset({'K'}))
    assert index.is_dispatched('sample.py', 'K', 2) is True


def test_separate_literals_are_not_evidence_of_a_reference():
    source = b'first="fo"; second="o"'
    index = value_references.build_reference_index([('sample.py', source)], names=frozenset({'foo'}))
    assert index.names_a_value('foo') is False


def test_empty_scope_still_skips_every_source():
    index = value_references.build_reference_index(_sources(), names=frozenset())
    assert index.parsed_sources == 0
    assert index.skipped_sources == 2


@pytest.mark.parametrize('prefix', ['', 'r', 'u', 'f', 'rf', 'fr', 't', 'rt', 'tr'])
@pytest.mark.parametrize('quote', ["'", '"', "'''", '"""'])
def test_literal_prefix_and_quote_context_keep_concatenated_names(prefix, quote):
    tail_prefix = 't' if 't' in prefix else ''
    source = f'lookup={prefix}{quote}fo{quote} {tail_prefix}"o"'.encode()
    full = value_references.build_reference_index([('sample.py', source)])
    narrowed = value_references.build_reference_index([('sample.py', source)], names=frozenset({'foo'}))
    assert full.names_a_value('foo') is True
    assert narrowed.names_a_value('foo') is True


def test_tokenization_failure_defers_to_the_existing_reader(monkeypatch):
    def refused(*_args):
        raise SyntaxError('synthetic tokenizer refusal')

    monkeypatch.setattr(value_references.tokenize, 'tokenize', refused)
    index = value_references.build_reference_index([('sample.py', b'lookup="fo" "o"')],
                                                  names=frozenset({'foo'}))
    assert index.names_a_value('foo') is True
    assert index.parsed_sources == 1
