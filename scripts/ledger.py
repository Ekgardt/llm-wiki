"""Compatible ledger entry and runtime generation/Markdown coordination.

Deterministic rules have one implementation in llm_wiki.ledger_core. Explicit
exports keep existing consumers and module-qualified historical objects usable.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from llm_wiki.ledger_core import (
    _DAILY_DAY as _DAILY_DAY,
)
from llm_wiki.ledger_core import (
    _DAY as _DAY,
)
from llm_wiki.ledger_core import (
    _FIELD_ODDS as _FIELD_ODDS,
)
from llm_wiki.ledger_core import (
    _INSERT as _INSERT,
)
from llm_wiki.ledger_core import (
    _PLURALS as _PLURALS,
)
from llm_wiki.ledger_core import (
    _SELECT as _SELECT,
)
from llm_wiki.ledger_core import (
    _UNDISTINGUISHING as _UNDISTINGUISHING,
)
from llm_wiki.ledger_core import (
    COLUMNS as COLUMNS,
)
from llm_wiki.ledger_core import (
    CONFIRMED as CONFIRMED,
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
    PROBABLE as PROBABLE,
)
from llm_wiki.ledger_core import (
    SAME_EVENT_DAYS as SAME_EVENT_DAYS,
)
from llm_wiki.ledger_core import (
    THING_OVERLAP as THING_OVERLAP,
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
    _all_records as _all_records,
)
from llm_wiki.ledger_core import (
    _by_kind as _by_kind,
)
from llm_wiki.ledger_core import (
    _capture_day as _capture_day,
)
from llm_wiki.ledger_core import (
    _days_apart as _days_apart,
)
from llm_wiki.ledger_core import (
    _event_agrees as _event_agrees,
)
from llm_wiki.ledger_core import (
    _event_quantity as _event_quantity,
)
from llm_wiki.ledger_core import (
    _flattened as _flattened,
)
from llm_wiki.ledger_core import (
    _groups as _groups,
)
from llm_wiki.ledger_core import (
    _matching_unit as _matching_unit,
)
from llm_wiki.ledger_core import (
    _missing_lines as _missing_lines,
)
from llm_wiki.ledger_core import (
    _new_event_evidence as _new_event_evidence,
)
from llm_wiki.ledger_core import (
    _new_page as _new_page,
)
from llm_wiki.ledger_core import (
    _page_with_section as _page_with_section,
)
from llm_wiki.ledger_core import (
    _pointer as _pointer,
)
from llm_wiki.ledger_core import (
    _posted_once as _posted_once,
)
from llm_wiki.ledger_core import (
    _quantity_agrees as _quantity_agrees,
)
from llm_wiki.ledger_core import (
    _quantity_field as _quantity_field,
)
from llm_wiki.ledger_core import (
    _record_from_row as _record_from_row,
)
from llm_wiki.ledger_core import (
    _record_items as _record_items,
)
from llm_wiki.ledger_core import (
    _record_of as _record_of,
)
from llm_wiki.ledger_core import (
    _records_of_kind as _records_of_kind,
)
from llm_wiki.ledger_core import (
    _recurrence as _recurrence,
)
from llm_wiki.ledger_core import (
    _split_by_touch as _split_by_touch,
)
from llm_wiki.ledger_core import (
    _stated_day as _stated_day,
)
from llm_wiki.ledger_core import (
    _stem as _stem,
)
from llm_wiki.ledger_core import (
    _text_field as _text_field,
)
from llm_wiki.ledger_core import (
    _things_agree as _things_agree,
)
from llm_wiki.ledger_core import (
    _things_of_every_kind as _things_of_every_kind,
)
from llm_wiki.ledger_core import (
    _tier as _tier,
)
from llm_wiki.ledger_core import (
    _tokens as _tokens,
)
from llm_wiki.ledger_core import (
    _touches as _touches,
)
from llm_wiki.ledger_core import (
    _trim_word_start as _trim_word_start,
)
from llm_wiki.ledger_core import (
    _weight as _weight,
)
from llm_wiki.ledger_core import (
    _word_character as _word_character,
)
from llm_wiki.ledger_core import (
    _word_separators as _word_separators,
)
from llm_wiki.ledger_core import (
    _word_tokens as _word_tokens,
)
from llm_wiki.ledger_core import (
    canonical as canonical,
)
from llm_wiki.ledger_core import (
    canonical_kind as canonical_kind,
)
from llm_wiki.ledger_core import (
    count as count,
)
from llm_wiki.ledger_core import (
    day_of_path as day_of_path,
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
    has_ledger as has_ledger,
)
from llm_wiki.ledger_core import (
    match_score as match_score,
)
from llm_wiki.ledger_core import (
    pointer_line as pointer_line,
)
from llm_wiki.ledger_core import (
    post as post,
)
from llm_wiki.ledger_core import (
    reconcile as reconcile,
)
from llm_wiki.ledger_core import (
    records_of as records_of,
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
    sum_quantities as sum_quantities,
)
from llm_wiki.ledger_core import (
    thing_agrees as thing_agrees,
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
    from generation_catalog import GenerationCatalog

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
    from memory_state import STATE_ROOT

    return STATE_ROOT


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
    for recurrence in recurring(connection):
        if changed >= limit:
            break
        change = _page_change(vault, recurrence, today)
        if change is None:
            continue
        mutate_knowledge(change[0], change[1], preconditions=change[2])
        changed += 1
    return changed
