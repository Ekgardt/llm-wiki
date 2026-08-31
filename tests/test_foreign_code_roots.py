"""The corpus collector's code-root allowlist belongs to its caller.

`corpus_snapshot.APPROVED_CODE_ROOTS` was the last place foreign-repository
indexing assumed this vault's own directory names. These tests fix the new
boundary: the *names* are the caller's, the *shape* and the walk's prune rule
are not. Design and measurement:
`docs/research/2026-08-29-which-directories-of-a-repository-hold-code.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import corpus_snapshot  # noqa: E402
from corpus_snapshot import collect_corpus, collectable_root_name  # noqa: E402

ALPHA = "def helper(value):\n    return value + 1\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    (root / "knowledge/projects").mkdir(parents=True)
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_root_outside_this_vaults_names_is_collected_when_the_caller_allows_it(
    vault: Path,
):
    """`src/` is a repository's code; it is not this vault's word for it."""
    _write(vault / "src/alpha.py", ALPHA)

    snapshot = collect_corpus(
        vault, code_roots=("src",), approved_code_roots=("src",)
    )

    assert [source.record.relative_path for source in snapshot.sources] == [
        "src/alpha.py"
    ]


def test_the_default_allowlist_is_unchanged_for_this_vault(vault: Path):
    """The vault's own call is byte-identical in behaviour: `src/` still refused."""
    _write(vault / "src/alpha.py", ALPHA)

    with pytest.raises(ValueError, match="code root"):
        collect_corpus(vault, code_roots=("src",))

    assert corpus_snapshot.APPROVED_CODE_ROOTS == frozenset(
        {"benchmark", "docs", "integrations", "rules", "scripts", "skills", "tests"}
    )


def test_the_caller_cannot_hand_over_the_path_shape_invariants(vault: Path):
    """Only the name list moves. Traversal, absolutes and Windows shapes do not."""
    _write(vault / "src/alpha.py", ALPHA)

    for unsafe in ("../src", "/src", "src\\alpha", "./src"):
        with pytest.raises(ValueError, match="code root"):
            collect_corpus(
                vault, code_roots=(unsafe,), approved_code_roots=(unsafe, "src")
            )


def test_a_root_the_walk_would_prune_is_refused_even_when_the_caller_allows_it(
    vault: Path,
):
    """The walk prunes a root's hidden children but is handed the root itself.

    So `.claude` as a code root would walk `.claude/worktrees/` -- a second copy
    of the repository, the defect closed in commit 1d06e6a. Collecting the
    wrong tree and collecting nothing are both worse than a named refusal.
    """
    _write(vault / ".claude/worktrees/agent-a/alpha.py", ALPHA)

    with pytest.raises(ValueError, match="the corpus walk prunes"):
        collect_corpus(
            vault, code_roots=(".claude",), approved_code_roots=(".claude",)
        )


_ROOT_NAME_CASES = {
    "src": True,
    "vepkit": True,
    "seed": True,
    ".github": False,
    ".claude": False,
    "__pycache__": False,
    "archive": False,
    "../src": False,
    "src/inner": False,
}


def test_collectable_root_name_answers_for_hidden_archive_and_skipped_names():
    answers = {name: collectable_root_name(name) for name in _ROOT_NAME_CASES}

    assert answers == _ROOT_NAME_CASES


def test_the_prune_rule_has_one_definition(vault: Path):
    """The predicate and the walk must not be able to disagree."""
    discovery = corpus_snapshot._Discovery(  # noqa: SLF001
        vault,
        max_files=10,
        max_entries=10,
        max_directories=10,
        max_depth=4,
        max_file_bytes=1024,
        max_total_bytes=1024,
        deadline=None,
        include_archives=False,
    )
    for name in (".github", "__pycache__", "archive", "src", "gaps"):
        assert discovery._directory_excluded(  # noqa: SLF001
            name, "code"
        ) is not collectable_root_name(name)


_WALK_KINDS = ("note", "project", "code")


def _pruned_by_kind(discovery, names: list[str]) -> dict[tuple[str, str], bool]:
    pairs = [(name, kind) for name in names for kind in _WALK_KINDS]
    return {
        pair: discovery._directory_excluded(*pair)  # noqa: SLF001
        for pair in pairs
    }


def _expected_by_kind(
    names: list[str], *, knowledge_only: bool
) -> dict[tuple[str, str], bool]:
    """`knowledge_only` names prune on the knowledge walks and nowhere else."""
    pairs = [(name, kind) for name in names for kind in _WALK_KINDS]
    return {pair: pair[1] != "code" or not knowledge_only for pair in pairs}


def _collectable_answers(names: list[str]) -> dict[str, bool]:
    return {name: collectable_root_name(name) for name in names}


def test_the_vault_vocabulary_prunes_knowledge_and_not_somebody_elses_code(
    vault: Path,
):
    """`gaps` is an OKF page type here and an ordinary directory anywhere else.

    Losing it inside a foreign repository would be silent -- no refusal, no
    `excluded_roots` entry, no line in the receipt. That is NEW-67's shape.
    """
    discovery = corpus_snapshot._Discovery(  # noqa: SLF001
        vault,
        max_files=10,
        max_entries=10,
        max_directories=10,
        max_depth=4,
        max_file_bytes=1024,
        max_total_bytes=1024,
        deadline=None,
        include_archives=False,
    )
    vocabulary = sorted(corpus_snapshot.VAULT_SKIP_DIRECTORIES)
    universal = ["__pycache__", ".github", "archive"]
    measured = _pruned_by_kind(discovery, vocabulary + universal)
    expected = _expected_by_kind(vocabulary, knowledge_only=True)
    expected.update(_expected_by_kind(universal, knowledge_only=False))

    assert measured == expected
    assert _collectable_answers(vocabulary) == dict.fromkeys(vocabulary, True)
