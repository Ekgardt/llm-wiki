"""A path rule cannot catch private content written inside a public page.

`.gitignore` denies every knowledge directory and names 91 published pages by
hand, so a *new* page is private by default. That half works. What it cannot do
is notice private material written into a page that is already published — and
on 2026-09-02 that is exactly what had happened: the name of another of the
owner's projects appeared twice in a published decision page and once in the
changelog, describing whose queue had jammed.

The vault knows the names of the owner's other projects: they are the
directories under `knowledge/projects/`. Any tracked page naming one of them,
or carrying a home path or the owner's address, is a leak that no path rule
would ever see.

This test fails closed. It reads what git actually tracks, not what the
`.gitignore` intends.

See `docs/research/2026-09-02-what-belongs-in-the-repository-and-what-the-undo-trail-should-be.md`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Slugs that belong to this product and appear in published pages on purpose,
# plus the short generic ones a project directory can carry.
OWN_SLUGS = frozenset(
    {"llm-wiki", "main", "user", "tmp", "docs", "_template", "checkout-claude"}
)

# A private path on the machine, and the owner's address. Neither belongs in a
# page anyone can clone.
PRIVATE_PATTERNS = (
    re.compile(r"/home/[a-z0-9_-]+/", re.IGNORECASE),
    re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE),
)

# Addresses that are a public identity rather than the owner's: the co-authorship
# line, and the address of the hosting service in a clone command.
PUBLIC_ADDRESSES = frozenset({"noreply@anthropic.com", "git@github.com"})

# Domains RFC 2606 reserves for documentation and testing, which is what a fixture
# address in this repository is: "`.example` is recommended for use in documentation
# or as examples", "`.invalid` is intended for use in online construction of domain
# names that are sure to be invalid", "`.test` is recommended for use in testing",
# and IANA "has the following second level domain names reserved which can be used
# as examples. example.com example.net example.org".
RESERVED_DOMAINS = (".example", ".invalid", ".test", ".localhost", "example.com", "example.net", "example.org")


# Text the whole repository is made of. A leak is a leak in a document, a comment
# or a fixture; the audit that first looked outside `knowledge/` found the name of
# another project in a document and real machine paths in seven test files, while
# this guard was green. See
# `docs/research/2026-09-18-nothing-personal-reaches-the-public-repository.md`.
TEXT_SUFFIXES = frozenset(
    {".md", ".py", ".json", ".yaml", ".yml", ".toml", ".sh", ".ps1", ".js", ".txt", ".cfg"}
)

# The two files whose purpose is to hold the pattern: this guard's own examples,
# and the fixture that proves the DLP rule catches a home path. Both are synthetic.
PATTERN_FIXTURES = frozenset(
    {"tests/test_nothing_private_reaches_the_public_repository.py", "tests/test_structure.py"}
)


def _tracked(prefix: str | None = None) -> list[Path]:
    arguments = ["git", "ls-files"] + ([prefix] if prefix else [])
    listing = subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [ROOT / line for line in listing.splitlines() if line]


def _tracked_knowledge() -> list[Path]:
    return [path for path in _tracked("knowledge/") if path.suffix == ".md"]


def _repository_path(path: Path) -> str:
    """The path as git spells it: forward slashes, on Windows as much as here.

    `str(Path)` spells a separator the way the host does, so on Windows the
    fixture exclusions below matched nothing, this guard swept its own examples
    and failed on itself. A repository path is a POSIX path everywhere.
    """
    return path.relative_to(ROOT).as_posix()


def _tracked_text() -> list[Path]:
    """Every tracked text file except the two that hold the pattern on purpose."""
    named = (path for path in _tracked() if path.suffix in TEXT_SUFFIXES)
    return [path for path in named if _repository_path(path) not in PATTERN_FIXTURES]


def _text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _offenders(finder) -> dict[str, set[str]]:
    """Each tracked text file that carries something private, and what it carries."""
    found = ((path, finder(_text_of(path))) for path in _tracked_text())
    return {_repository_path(path): hit for path, hit in found if hit}


# A run of this product creates project directories of its own: a benchmark corpus,
# a dated run label, a generated project key. Those are this repository's own
# vocabulary, not somebody else's project, and they are told apart by what the
# repository itself holds — never by a list of private names.
GENERATED_SLUG = re.compile(r"^[0-9a-f]{16,}$|\d{4}-\d{2}-\d{2}")


def _repository_vocabulary() -> set[str]:
    """Words this repository already uses in its own tracked paths."""
    words: set[str] = set()
    for path in _tracked():
        words.update(re.split(r"[/._-]", _repository_path(path).casefold()))
    return words


def _is_other_project(entry: Path, vocabulary: set[str]) -> bool:
    """A directory that names one of the owner's other projects, not this one."""
    if not entry.is_dir() or entry.name in OWN_SLUGS or len(entry.name) <= 4:
        return False
    if GENERATED_SLUG.search(entry.name):
        return False
    return entry.name.casefold() not in vocabulary


def _other_project_slugs() -> set[str]:
    projects = ROOT / "knowledge" / "projects"
    if not projects.is_dir():
        return set()
    vocabulary = _repository_vocabulary()
    return {entry.name for entry in projects.iterdir() if _is_other_project(entry, vocabulary)}


def _named_slugs(text: str, slugs: set[str]) -> set[str]:
    """The slugs this text names as a token — not as two words inside a longer phrase.

    `\\b` counts a hyphen as a boundary, so a project called `refusal-names` matched the
    prose in `...-a-refusal-names-its-component-...`. A leak names a project in a path, in
    quotes or after a space. See
    `docs/research/2026-09-18-a-project-slug-is-a-name-not-a-phrase.md`.
    """
    return {
        slug
        for slug in slugs
        if re.search(rf"(?<![\w-]){re.escape(slug)}(?![\w-])", text)
    }


def _is_private(match: str) -> bool:
    """A home path always; an address unless it is public or a reserved example domain."""
    if match in PUBLIC_ADDRESSES:
        return False
    return not match.casefold().endswith(RESERVED_DOMAINS)


def _private_strings(text: str) -> set[str]:
    found: set[str] = set()
    for pattern in PRIVATE_PATTERNS:
        found.update(match for match in pattern.findall(text) if _is_private(match))
    return found


def test_no_tracked_file_names_another_project() -> None:
    """The leak that happened: a neighbouring project named in a file anyone can clone.

    Documents, comments and fixtures, not only published pages: the names were in a
    developer document, five research notes, four source comments and three tests.
    """
    slugs = _other_project_slugs()

    offenders = _offenders(lambda text: _named_slugs(text, slugs))

    assert not offenders, f"tracked files name other projects: {offenders}"


def test_no_tracked_file_carries_a_home_path_or_an_address() -> None:
    offenders = _offenders(_private_strings)

    assert not offenders, f"tracked files carry private strings: {offenders}"


def test_the_check_reads_git_rather_than_the_ignore_file() -> None:
    """An intention is not a fact; only what git tracks can leak."""
    tracked = _tracked_knowledge()

    assert tracked, "expected published knowledge pages to exist"
    assert all(path.is_file() for path in tracked)


def test_the_vault_knows_which_projects_are_not_its_own() -> None:
    """Without that list the first check is vacuous, so say when it is.

    A clean checkout has no private project directories — they are gitignored,
    which is the whole point — so this can only be asserted where they exist.
    On CI it skips and says so rather than failing; on the owner's vault it is
    the guard against a silent pass. It failed on every platform in CI until
    2026-09-05 for exactly that reason, which is a test that did not know where
    it was.
    """
    if not _other_project_slugs():
        pytest.skip("clean checkout: no private projects to name")

    assert _other_project_slugs()


def test_a_planted_slug_would_be_caught() -> None:
    """The guard must fail on the thing it exists to catch.

    Planted rather than real, so this runs in a clean checkout too: what is
    under test is the matcher, not the contents of one machine.
    """
    slugs = {"someone-elses-project", "another-private-thing"}

    found = _named_slugs("the queue for someone-elses-project had jammed", slugs)

    assert found == {"someone-elses-project"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a-private-thing", True),          # somebody's project
        ("full-2026-09-14", False),         # a dated run label of this product
        ("cb387b9645434586a379f252ac", False),  # a generated project key
        ("benchmark", False),               # a directory of this repository
        ("refusalbench", False),            # a corpus this repository already names
    ],
)
def test_the_product_own_runs_are_not_other_projects(tmp_path, name, expected) -> None:
    """A run of this product creates project directories too; only the rest can leak."""
    (tmp_path / name).mkdir()

    assert _is_other_project(tmp_path / name, _repository_vocabulary()) is expected


def test_a_slug_inside_a_longer_word_is_not_a_match() -> None:
    """Word boundaries, or every page mentioning `api` names a project."""
    assert _named_slugs("the rapid queue", {"api"}) == set()


def test_a_planted_home_path_would_be_caught() -> None:
    assert _private_strings("see /home/someone/vault/run") == {"/home/someone/"}


def test_a_reserved_example_address_is_not_a_leak() -> None:
    """RFC 2606 domains are what a fixture address in this repository is."""
    fixtures = "t@example.invalid parity@example.test someone@example.com git@github.com"

    assert _private_strings(fixtures) == set()


def test_a_real_looking_address_is_still_caught() -> None:
    assert _private_strings("write to someone@somewhere.org") == {"someone@somewhere.org"}


def test_the_sweep_reaches_past_the_knowledge_directory() -> None:
    """The names were in documents, comments and fixtures, not in published pages."""
    swept = {_repository_path(path) for path in _tracked_text()}
    zones = {PurePosixPath(path).parts[0] for path in swept}

    assert {"docs", "scripts", "tests", "benchmark"} <= zones
    assert "tests/test_structure.py" not in swept


def test_the_guard_spells_a_repository_path_the_way_git_spells_it() -> None:
    """git prints `tests/x.py` on every platform; `str(Path)` does not.

    The exclusions and the zone names below are written in git's spelling, so
    every path this guard compares has to be spelled that way too. On Windows,
    while it was not, the two fixture files swept themselves and the guard
    reported its own examples as a leak.
    """
    spelled = _repository_path(ROOT / "tests" / "test_structure.py")

    assert (spelled, spelled in PATTERN_FIXTURES) == ("tests/test_structure.py", True)


def test_the_projects_own_address_is_not_a_leak() -> None:
    """Co-authorship lines are public identity, not the owner's address."""
    assert _private_strings("Co-Authored-By: Claude <noreply@anthropic.com>") == set()


def test_a_slug_is_named_as_a_token_and_not_as_two_words_of_a_phrase() -> None:
    """A two-word project slug must not match prose written in slug form.

    See `docs/research/2026-09-18-a-project-slug-is-a-name-not-a-phrase.md`.
    """
    slugs = {"refusal-names"}
    leaks = ("knowledge/projects/refusal-names/state.md", 'the "refusal-names" project', "refusal-names.md")
    phrase = "docs/research/2026-09-18-lsp-a-refusal-names-its-component-and-its-rule.md"

    assert [_named_slugs(text, slugs) for text in leaks] == [slugs] * len(leaks)
    assert _named_slugs(phrase, slugs) == set()
