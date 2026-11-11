"""Compatible ledger entry and runtime generation/Markdown coordination.

Deterministic rules have one implementation in llm_wiki.ledger_core. Explicit
exports keep existing consumers and module-qualified historical objects usable.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from llm_wiki import runtime_roots
from llm_wiki.ledger_core import (
    COLUMNS as COLUMNS,
)
from llm_wiki.ledger_core import (
    CONFIRMED as CONFIRMED,
)
from llm_wiki.ledger_core import (
    DAILY_DAY as DAILY_DAY,
)
from llm_wiki.ledger_core import (
    DAY as DAY,
)
from llm_wiki.ledger_core import (
    FIELD_ODDS as FIELD_ODDS,
)
from llm_wiki.ledger_core import (
    INSERT as INSERT,
)
from llm_wiki.ledger_core import (
    LEDGER_DDL as LEDGER_DDL,
)
from llm_wiki.ledger_core import (
    LEDGER_SCHEMA_VERSION as LEDGER_SCHEMA_VERSION,
)
from llm_wiki.ledger_core import (
    LEDGER_SECTION as LEDGER_SECTION,
)
from llm_wiki.ledger_core import (
    LEDGER_TABLE as LEDGER_TABLE,
)
from llm_wiki.ledger_core import (
    MATCH_THRESHOLD as MATCH_THRESHOLD,
)
from llm_wiki.ledger_core import (
    MAX_EVENT_CHARS as MAX_EVENT_CHARS,
)
from llm_wiki.ledger_core import (
    MAX_KIND_CHARS as MAX_KIND_CHARS,
)
from llm_wiki.ledger_core import (
    MAX_THING_CHARS as MAX_THING_CHARS,
)
from llm_wiki.ledger_core import (
    PLURALS as PLURALS,
)
from llm_wiki.ledger_core import (
    PROBABLE as PROBABLE,
)
from llm_wiki.ledger_core import (
    SAME_EVENT_DAYS as SAME_EVENT_DAYS,
)
from llm_wiki.ledger_core import (
    SELECT as SELECT,
)
from llm_wiki.ledger_core import (
    THING_OVERLAP as THING_OVERLAP,
)
from llm_wiki.ledger_core import (
    UNDISTINGUISHING as UNDISTINGUISHING,
)
from llm_wiki.ledger_core import (
    LedgerCount as LedgerCount,
)
from llm_wiki.ledger_core import (
    Pointer as Pointer,
)
from llm_wiki.ledger_core import (
    Reconciliation as Reconciliation,
)
from llm_wiki.ledger_core import (
    Record as Record,
)
from llm_wiki.ledger_core import (
    Recurrence as Recurrence,
)
from llm_wiki.ledger_core import (
    all_records as all_records,
)
from llm_wiki.ledger_core import (
    canonical as canonical,
)
from llm_wiki.ledger_core import (
    canonical_kind as canonical_kind,
)
from llm_wiki.ledger_core import (
    capture_day as capture_day,
)
from llm_wiki.ledger_core import (
    count as count,
)
from llm_wiki.ledger_core import (
    day_of_path as day_of_path,
)
from llm_wiki.ledger_core import (
    days_apart as days_apart,
)
from llm_wiki.ledger_core import (
    ensure_table as ensure_table,
)
from llm_wiki.ledger_core import (
    entity_page_bytes as entity_page_bytes,
)
from llm_wiki.ledger_core import (
    entity_slug as entity_slug,
)
from llm_wiki.ledger_core import (
    event_agrees as event_agrees,
)
from llm_wiki.ledger_core import (
    event_quantity as event_quantity,
)
from llm_wiki.ledger_core import (
    flattened as flattened,
)
from llm_wiki.ledger_core import (
    groups as groups,
)
from llm_wiki.ledger_core import (
    has_ledger as has_ledger,
)
from llm_wiki.ledger_core import (
    match_score as match_score,
)
from llm_wiki.ledger_core import (
    matching_unit as matching_unit,
)
from llm_wiki.ledger_core import (
    missing_lines as missing_lines,
)
from llm_wiki.ledger_core import (
    new_event_evidence as new_event_evidence,
)
from llm_wiki.ledger_core import (
    new_page as new_page,
)
from llm_wiki.ledger_core import (
    page_with_section as page_with_section,
)
from llm_wiki.ledger_core import (
    pointer as pointer,
)
from llm_wiki.ledger_core import (
    pointer_line as pointer_line,
)
from llm_wiki.ledger_core import (
    post as post,
)
from llm_wiki.ledger_core import (
    posted_once as posted_once,
)
from llm_wiki.ledger_core import (
    quantity_agrees as quantity_agrees,
)
from llm_wiki.ledger_core import (
    quantity_field as quantity_field,
)
from llm_wiki.ledger_core import (
    reconcile as reconcile,
)
from llm_wiki.ledger_core import (
    record_from_row as record_from_row,
)
from llm_wiki.ledger_core import (
    record_items as record_items,
)
from llm_wiki.ledger_core import (
    record_of as record_of,
)
from llm_wiki.ledger_core import (
    records_by_kind as records_by_kind,
)
from llm_wiki.ledger_core import (
    records_of as records_of,
)
from llm_wiki.ledger_core import (
    records_of_kind as records_of_kind,
)
from llm_wiki.ledger_core import (
    recurrence as recurrence,
)
from llm_wiki.ledger_core import (
    recurring as recurring,
)
from llm_wiki.ledger_core import (
    rows as rows,
)
from llm_wiki.ledger_core import (
    rows_in_store as rows_in_store,
)
from llm_wiki.ledger_core import (
    same_event as same_event,
)
from llm_wiki.ledger_core import (
    split_by_touch as split_by_touch,
)
from llm_wiki.ledger_core import (
    stated_day as stated_day,
)
from llm_wiki.ledger_core import (
    stem as stem,
)
from llm_wiki.ledger_core import (
    sum_quantities as sum_quantities,
)
from llm_wiki.ledger_core import (
    text_field as text_field,
)
from llm_wiki.ledger_core import (
    thing_agrees as thing_agrees,
)
from llm_wiki.ledger_core import (
    things_agree as things_agree,
)
from llm_wiki.ledger_core import (
    things_of_every_kind as things_of_every_kind,
)
from llm_wiki.ledger_core import (
    tier as tier,
)
from llm_wiki.ledger_core import (
    tokens as tokens,
)
from llm_wiki.ledger_core import (
    touches as touches,
)
from llm_wiki.ledger_core import (
    trim_word_start as trim_word_start,
)
from llm_wiki.ledger_core import (
    weight as weight,
)
from llm_wiki.ledger_core import (
    word_character as word_character,
)
from llm_wiki.ledger_core import (
    word_separators as word_separators,
)
from llm_wiki.ledger_core import (
    word_tokens as word_tokens,
)
from llm_wiki.ledger_core import (
    write_table as write_table,
)

# Compatibility default of extend_entity_pages(limit=20), retained under the
# approved behavior-preserving decomposition. Callers can override limit; no
# calibration evidence was found for 20. Revisit with publication workload evidence.
MAX_PAGES_PER_RUN = 20


def active_generation_search(state_root: Path) -> Path | None:
    """The active generation's search artifact, or None when there is no generation."""
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog_path = Path(state_root) / "cache" / "evidence-graph" / "catalog.sqlite3"
    if not catalog_path.exists():
        return None
    try:
        catalog = GenerationCatalog(state_root, catalog_path=catalog_path)
        active = catalog.get_active()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    return _artifact_of(catalog, active)


def _artifact_of(catalog: object, active: object) -> Path | None:
    if not isinstance(active, Mapping) or not isinstance(active.get("generation_id"), str):
        return None
    artifact = Path(catalog.generations_path) / active["generation_id"] / "search.sqlite3"
    if not artifact.is_file():
        return None
    return artifact


def _default_state_root(state_root: Path | None) -> Path:
    if state_root is not None:
        return state_root

    return runtime_roots.state_root()


def _read_active_generation(state_root: Path | None, reader: Callable) -> object:
    """`reader(connection)` over the active generation's search artifact, read-only."""
    artifact = active_generation_search(_default_state_root(state_root))
    if artifact is None:
        return None
    connection = sqlite3.connect(f"{artifact.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        return reader(connection)
    finally:
        connection.close()


def count_in_active_generation(
    kind: str, window: tuple[str, str] | None = None, state_root: Path | None = None
) -> LedgerCount | None:
    """`count` over the active generation; None when there is no generation or no ledger.

    This is the reader's call: no provider, no token, one read-only SQLite query.
    """
    return _read_active_generation(state_root, lambda connection: count(connection, kind, window))


def sum_in_active_generation(
    kind: str, window: tuple[str, str] | None = None, state_root: Path | None = None
) -> float | None:
    """`sum_quantities` over the active generation, for "how much in total"; None without a ledger."""
    return _read_active_generation(
        state_root, lambda connection: sum_quantities(connection, kind, window)
    )


def _existing_page(page: Path) -> bytes | None:
    from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES, read_stable_bytes

    if not page.exists():
        return None
    return read_stable_bytes(page, MAX_KNOWLEDGE_PAGE_BYTES, label="ledger entity page")


def _page_change(vault: Path, recurrence: Recurrence, today: str):
    """(operation id, change, precondition) for one page, or None when it is current."""
    from llm_wiki.formats.canonical import sha256_bytes
    from markdown_transaction import stable_operation_id

    relative = f"knowledge/notes/{entity_slug(recurrence)}.md"
    page = Path(vault) / relative
    existing = _existing_page(page)
    content = entity_page_bytes(existing, recurrence, today)
    if content is None:
        return None
    precondition = {} if existing is None else {relative: sha256_bytes(existing)}
    return stable_operation_id("ledger-entity", relative, content), {page: content}, precondition


def extend_entity_pages(
    vault: Path, connection: sqlite3.Connection, today: str, limit: int = MAX_PAGES_PER_RUN
) -> int:
    """Create or extend the page of every recurring thing; how many pages changed.

    Each page is one recoverable Markdown transaction through `mutate_knowledge`,
    bounded to `limit` pages a run; the rest wait for the next night.
    """
    from markdown_transaction import mutate_knowledge

    changed = 0
    for recurring_row in recurring(connection):
        if changed >= limit:
            break
        change = _page_change(vault, recurring_row, today)
        if change is None:
            continue
        mutate_knowledge(change[0], change[1], preconditions=change[2])
        changed += 1
    return changed
