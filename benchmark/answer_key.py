"""Where a stand's answer sheet lives — found by content, never by a literal.

Both vault stands keep their questions and their gold pages in one JSON file
under `benchmark/`, and `benchmark` is an approved corpus root, so the sheet is
indexed with everything else. A stand that can retrieve its own sheet is a
stand that can score by finding itself.

Each stand used to name one path in a module constant. That constant went
stale the way constants do: `run_vault_application` dropped the *retrieval*
sheet from its ranking and left its own sheet in, and neither stand knew about
`tests/test_intent_conditional_trust.py`, which pins one case's question next
to that case's gold page.

So the set is derived instead of declared. A file belongs to the answer key
when it states a case in the stand's own words — the question or task string,
verbatim. That is the property that makes retrieving it cheating, and it is
checked against the files on disk every run, so a new copy of the sheet joins
the set the day it appears rather than the day someone remembers.

A gold page can never be excluded. Dropping one would zero its case and the
stand would read as if retrieval had failed, so the gold pages are subtracted
from the derived set as a floor.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from corpus_snapshot import APPROVED_CODE_ROOTS  # noqa: E402

# Where retrieval can answer from. The code roots are read off the collector
# rather than copied, and the two knowledge roots are the ones the active
# generation actually carries: `knowledge/daily` and `knowledge/raw/sessions`
# are on disk but deliberately outside the corpus.
KNOWLEDGE_ROOTS = ("knowledge/notes", "knowledge/projects")
# Large enough for every source the collector admits; a file past this is not
# read, and the collector does not index it either.
MAX_SCANNED_BYTES = 2 * 1024 * 1024
_SKIP_DIRECTORIES = frozenset({"__pycache__"})


def scan_roots() -> tuple[str, ...]:
    """The roots a retrieved path can come from, in a stable order."""
    return tuple(sorted(APPROVED_CODE_ROOTS)) + KNOWLEDGE_ROOTS


def phrases(corpus: dict) -> tuple[str, ...]:
    """Every case stated in the words the stand will ask it in."""
    asked = ("question", "task")
    return tuple(
        str(case[key]) for case in corpus["cases"] for key in asked if key in case
    )


def gold_paths(corpus: dict) -> frozenset[str]:
    return frozenset(str(case["gold_path"]) for case in corpus["cases"])


def _text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_SCANNED_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _wanted(path: Path) -> bool:
    return path.is_file() and _SKIP_DIRECTORIES.isdisjoint(path.parts)


def _files_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if _wanted(path))


def scanned_files(vault: Path) -> list[Path]:
    found: list[Path] = []
    for relative in scan_roots():
        found.extend(_files_under(vault / relative))
    return found


def _states_a_case(text: str, asked: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in asked)


def leaking_paths(vault: Path, asked: tuple[str, ...]) -> frozenset[str]:
    """Vault-relative paths of every indexed file that states a case verbatim."""
    return frozenset(
        path.relative_to(vault).as_posix()
        for path in scanned_files(vault)
        if _states_a_case(_text(path), asked)
    )


def answer_key_paths(vault: Path, corpora: tuple[dict, ...]) -> frozenset[str]:
    """The sheets both stands must refuse to score from.

    The union of both corpora, because a sheet crowds the ranking of the stand
    it does not belong to just as hard as its own.
    """
    asked = tuple(phrase for corpus in corpora for phrase in phrases(corpus))
    golds = frozenset().union(frozenset(), *(gold_paths(corpus) for corpus in corpora))
    return leaking_paths(vault, asked) - golds


# Every corpus whose cases can be found by retrieving the file that states them.
STAND_CORPORA = (
    "benchmark/vault-retrieval-v1.json",
    "benchmark/vault-application-v1.json",
)
_CACHE: dict[str, frozenset[str]] = {}


def _loaded(path: Path) -> dict | None:
    """A corpus that is not in this tree is not a sheet this tree can leak."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stand_corpora(vault: Path) -> tuple[dict, ...]:
    found = (_loaded(vault / relative) for relative in STAND_CORPORA)
    return tuple(corpus for corpus in found if corpus is not None)


def sheets(vault: Path) -> frozenset[str]:
    """The derived answer key for this vault, scanned once per process.

    Scanning is a third of a second over about nine hundred files. Cheap, but
    not cheap enough to repeat for every case of every run.
    """
    cached = _CACHE.get(str(vault))
    if cached is None:
        cached = answer_key_paths(vault, stand_corpora(vault))
        _CACHE[str(vault)] = cached
    return cached
