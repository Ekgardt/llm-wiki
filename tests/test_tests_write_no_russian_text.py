"""No test writes Russian text; Cyrillic words appear only where Cyrillic is the input.

Law 10 keeps Russian out of the code. A test that needs one multi-byte letter
for its width writes that single code point. A test whose subject is Cyrillic
input builds its words from code points, and its file is named below with the
behaviour that makes the Cyrillic necessary. Everywhere else the data is
English or another script. Writing a Russian phrase as `\\u` escapes does not
make it English: a new file that does so fails here until its data is replaced
or its reason is recorded.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CYRILLIC_LETTER = re.compile("[\u0400-\u04ff]")
CYRILLIC_WORD = re.compile("[\u0400-\u04ff]{2,}")

_RUSSIAN_LOCALE_WINDOWS = (
    "a Russian-locale Windows prints account names in OEM code page 866, "
    "which encodes Cyrillic and not other scripts"
)
_RUSSIAN_QUERIES = "query analysis recognises Russian question forms and stopwords"
_CYRILLIC_SLUGS = "slugs keep Cyrillic letters and fold their case"
_RUSSIAN_DOCUMENTS = "the check quotes sentences of the published Russian documents"

CYRILLIC_IS_THE_INPUT = {
    "a_windows_owner_with_a_non_ascii_name_is_verified_helpers.py": _RUSSIAN_LOCALE_WINDOWS,
    "modules/windows_acl/test_acl.py": _RUSSIAN_LOCALE_WINDOWS,
    "test_a_windows_name_is_read_wherever_an_acl_is_checked.py": _RUSSIAN_LOCALE_WINDOWS,
    "test_a_windows_owner_with_a_non_ascii_name_is_verified.py": _RUSSIAN_LOCALE_WINDOWS,
    "test_runtime_publication_verifies_instead_of_rehardening.py": _RUSSIAN_LOCALE_WINDOWS,
    "modules/flush_corpus_labels/test_semantics.py": "the labeller tags Cyrillic transcripts RU",
    "test_corpus_snapshot.py": "the snapshot detects a Russian span and tags it ru",
    "test_retrieval_v2_benchmark.py": "lexical candidates are kept per language, RU among them",
    "test_retrieval_v2_scoring.py": "the FTS5 unicode61 tokenizer folds Cyrillic case",
    "test_a_grounded_answer_keeps_automatic_search.py": _RUSSIAN_QUERIES,
    "test_a_question_finds_pages_without_a_generation.py": _RUSSIAN_QUERIES,
    "test_intent_conditional_trust.py": _RUSSIAN_QUERIES,
    "test_retrieval.py": _RUSSIAN_QUERIES,
    "test_retrieval_review_blockers.py": _RUSSIAN_QUERIES,
    "test_retrieval_review_round2.py": _RUSSIAN_QUERIES,
    "test_semantic_leg_reaches_the_answer.py": _RUSSIAN_QUERIES,
    "test_project_journal.py": _CYRILLIC_SLUGS,
    "test_slug.py": _CYRILLIC_SLUGS,
    "test_slugify.py": _CYRILLIC_SLUGS,
    "test_quality_guards.py": _RUSSIAN_DOCUMENTS,
    "test_readme_i18n.py": _RUSSIAN_DOCUMENTS,
}


def _test_sources(root: Path) -> list[Path]:
    """Test modules and helpers; fixtures are input data, not code."""
    tests = root / "tests"
    return sorted(p for p in tests.rglob("*.py") if "fixtures" not in p.relative_to(tests).parts)


def _builds_cyrillic_words(source: str) -> bool:
    strings = (node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant))
    return any(isinstance(value, str) and CYRILLIC_WORD.search(value) for value in strings)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root / "tests").as_posix()


def test_no_test_source_writes_a_cyrillic_letter() -> None:
    written = [
        _relative(path, ROOT)
        for path in _test_sources(ROOT)
        if CYRILLIC_LETTER.search(path.read_text(encoding="utf-8"))
    ]

    assert written == []


def test_only_tests_of_cyrillic_input_build_cyrillic_words() -> None:
    building = {
        _relative(path, ROOT)
        for path in _test_sources(ROOT)
        if _builds_cyrillic_words(path.read_text(encoding="utf-8"))
    }

    assert building == set(CYRILLIC_IS_THE_INPUT)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('PHRASE = "\\u0434\\u0430"\n', True),
        ('WIDE = "\\u044f" * 256\n', False),
        ('RANGE = "[\\u0400-\\u04ff]"\n', False),
        ('PHRASE = "\\u4f60\\u597d"\n', False),
    ],
)
def test_an_escaped_word_is_still_a_word(source: str, expected: bool) -> None:
    assert _builds_cyrillic_words(source) is expected
