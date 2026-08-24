"""One definition of which pages have stopped being current.

Four places asked the same question and answered it two different ways. The lint
and the index map treat `superseded` and `archived` as retired and everything
else as current; the corpus collector and the query-time filters kept only
`active`. A decision page marked `accepted` — the word the decision practice this
vault writes in uses for "in force" — was therefore collected into nothing and
filtered out of every answer. Measured on this vault: nine of sixty-eight notes
were absent from the corpus, four of them decisions in force.

The vocabulary follows that practice: `proposed` and `accepted` are in force,
`superseded`, `deprecated` and `rejected` are not, and `archived` is this vault's
own word for a page its lifecycle rules retired. The rule is a closed list of
retired words rather than a closed list of current ones, so a status word nobody
anticipated leaves the page findable instead of invisible — for a memory system,
knowledge that cannot be found is the worse failure, and it is the one that
happened. See docs/research/2026-08-24-which-pages-count-as-current.md.
"""
from __future__ import annotations

RETIRED_STATUSES = frozenset({"superseded", "archived", "deprecated", "rejected"})

DEFAULT_STATUS = "active"


def normalized_status(value: object) -> str:
    """The page's status in one comparable form; absent or blank means active."""
    if not isinstance(value, str):
        return DEFAULT_STATUS
    text = value.strip().strip("`\"'").casefold()
    return text or DEFAULT_STATUS


def is_retired(value: object) -> bool:
    """Whether this status means the page is history rather than knowledge."""
    return normalized_status(value) in RETIRED_STATUSES


def current_status_sql(column: str = "status") -> str:
    """The same rule as SQL, built from the same set so the two cannot drift."""
    listed = ", ".join(f"'{status}'" for status in sorted(RETIRED_STATUSES))
    return f"({column} IS NULL OR {column} = '' OR lower({column}) NOT IN ({listed}))"
