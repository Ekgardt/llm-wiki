"""A ledger of things and events: posted once at compile, counted by code at question time.

Counting fails on an incomplete set, not on arithmetic: an answer built from the
best twelve chunks has a ceiling, because twelve is not "all" (GlobalQA; EC-Bench,
where enumeration F1 and counting accuracy move together, rho 0.692). LongMemEval
itself measured that representing values as extracted facts helps the
multi-session questions and hurts the others, so this ledger is an *additional*
index over the episodes and never replaces the text: the reader keeps reading
turns for everything else.

Each record is one thing or one dated event about it — kind, canonical thing,
event, day, quantity — with the same pointer to the source bytes the fact keys
carry. The records ride in the fact-keys reply of the nightly pass (one provider
call per twenty-five turns, already paid) and cost nothing at question time.

Three rules from other fields, each stated once here:

* **Post once.** The posting key is the digest of kind, thing, event, day and
  pointer, so a re-run posts nothing twice (accounting's double entry: an
  invariant that only holds when each entry is posted exactly once).
* **Same event.** Two records of one thing within `SAME_EVENT_DAYS` are one event
  unless there is evidence of a new one — the CDC's 2016 case de-duplication rule,
  where the majority of jurisdictions use 30 days. Within the window the pair is
  decided by a Fellegi–Sunter sum of field weights, a deterministic pre-merge
  before any model clustering.
* **Count by code, reconcile by code.** `count` returns the number, the tier
  ("confirmed" when every record is the user's own words with a stated date,
  "probable" otherwise, so an answer can say "at least N") and the pointers;
  `reconcile` compares the reader's number with the ledger's and flags a
  mismatch instead of trusting either.

And one warning from biology: a thing gets an entity page only when it recurs
(Sun et al. 2023), and the page is extended with dated pointer lines by code,
never rewritten by a model each night (Zhang et al. 2026).

Honest limit, from the trial balance: a ledger cannot count what capture never
wrote or what the extraction never posted.
Research: `docs/research/2026-09-22-a-ledger-of-things-and-events-posted-once.md`.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

LEDGER_SCHEMA_VERSION = "ledger/v1"
LEDGER_TABLE = "ledger"
# The CDC's majority de-duplication window: one infection is one case when the
# prior report lies within 30 days and nothing says it is a new one.
SAME_EVENT_DAYS = 30
MAX_KIND_CHARS = 40
MAX_THING_CHARS = 120
# One ledger event's text; a temporal anchor keeps up to 220 characters.
MAX_EVENT_CHARS = 120
MAX_RECORDS_PER_TURN = 8
MAX_PAGES_PER_RUN = 20
CONFIRMED = "confirmed"
PROBABLE = "probable"
LEDGER_SECTION = "## Ledger"
# Fellegi–Sunter field weights: ln(m/u) when a field agrees, ln((1-m)/(1-u)) when
# it does not, summed over fields; a pair is one event when the sum is not below
# the threshold. m is the chance the field agrees on a true match, u on a
# non-match. There is no labelled pair set to estimate them from, so they are
# stated here, and the threshold is the neutral likelihood ratio of one. The
# window and a contradicting stated quantity are not fields but vetoes: the
# CDC rule de-duplicates *unless there is evidence of a new event*.
_FIELD_ODDS = {"thing": (0.9, 0.1), "event": (0.8, 0.3)}
MATCH_THRESHOLD = 0.0
THING_OVERLAP = 0.5

LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE}(
    posting_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    thing TEXT NOT NULL,
    event TEXT NOT NULL,
    day TEXT NOT NULL,
    quantity REAL,
    by_user INTEGER NOT NULL,
    dated INTEGER NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    span_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_kind_day ON {LEDGER_TABLE}(kind, day);
"""
COLUMNS = (
    "posting_key",
    "kind",
    "thing",
    "event",
    "day",
    "quantity",
    "by_user",
    "dated",
    "source_path",
    "source_sha256",
    "byte_start",
    "byte_end",
    "span_sha256",
)
_SELECT = f"SELECT {', '.join(COLUMNS)} FROM {LEDGER_TABLE}"
_INSERT = f"INSERT OR IGNORE INTO {LEDGER_TABLE} VALUES ({', '.join('?' for _ in COLUMNS)})"
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAILY_DAY = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")
_UNDISTINGUISHING = frozenset(
    {"the", "a", "an", "my", "our", "his", "her", "their", "its", "of", "some", "this", "that"}
)
_PLURALS = (("ies", "y"), ("sses", "ss"), ("ss", "ss"), ("s", ""))


@dataclass(frozen=True)
class Record:
    """One thing, or one dated event about it, with the bytes it came from."""

    kind: str
    thing: str
    event: str
    day: str
    quantity: float | None
    by_user: bool
    dated: bool
    source_path: str
    source_sha256: str
    byte_start: int
    byte_end: int
    span_sha256: str

    @property
    def posting_key(self) -> str:
        pointer = f"{self.source_path}:{self.byte_start}-{self.byte_end}"
        joined = "|".join((self.kind, self.thing, self.event, self.day, pointer))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def row(self) -> tuple[object, ...]:
        return (
            self.posting_key,
            self.kind,
            self.thing,
            self.event,
            self.day,
            self.quantity,
            int(self.by_user),
            int(self.dated),
            self.source_path,
            self.source_sha256,
            self.byte_start,
            self.byte_end,
            self.span_sha256,
        )


@dataclass(frozen=True)
class Pointer:
    """Where a record came from: the fields `evidence_resolver` renders a reference from."""

    day: str
    event: str
    source_path: str
    source_sha256: str
    byte_start: int
    byte_end: int
    span_sha256: str


@dataclass(frozen=True)
class LedgerCount:
    """What the ledger holds for one kind in one window."""

    kind: str
    window: tuple[str, str] | None
    records: int
    events: int
    things: int
    tier: str
    pointers: tuple[Pointer, ...]


@dataclass(frozen=True)
class Reconciliation:
    """The reader's number set beside the ledger's."""

    stated: float | None
    counted: LedgerCount | None
    unit: str | None

    @property
    def agrees(self) -> bool | None:
        """True or False when both sides have a number, None when either is missing."""
        if self.stated is None or self.counted is None:
            return None
        return self.unit is not None


@dataclass(frozen=True)
class Recurrence:
    """A thing seen on two or more days: the recurrence gate is open for its page."""

    kind: str
    thing: str
    days: tuple[str, ...]
    pointers: tuple[Pointer, ...]


# --- canonical forms -------------------------------------------------------


def _stem(word: str) -> str:
    lowered = word.casefold().strip("'")
    for ending, replacement in _PLURALS:
        if lowered.endswith(ending) and len(lowered) > len(ending) + 1:
            return lowered[: -len(ending)] + replacement
    return lowered


def canonical(text: str) -> str:
    """The words that tell one thing from another, stemmed, in order."""
    words = (word.casefold() for word in _WORD.findall(text or ""))
    return " ".join(_stem(word) for word in words if word not in _UNDISTINGUISHING)


def canonical_kind(text: str) -> str:
    """The head noun of a kind, stemmed: "movie festivals" is a festival."""
    words = canonical(text).split()
    if not words:
        return ""
    return words[-1][:MAX_KIND_CHARS]


def day_of_path(source_path: str) -> str:
    """The day a daily file is about, or "" for any other source."""
    match = _DAILY_DAY.search(source_path or "")
    if match is None:
        return ""
    return match.group(1)


# --- reading records out of a reply --------------------------------------


def _text_field(item: Mapping[str, object], name: str, limit: int) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _quantity_field(item: Mapping[str, object]) -> float | None:
    value = item.get("quantity")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return float(value)


def _stated_day(item: Mapping[str, object]) -> str:
    value = item.get("date")
    if not isinstance(value, str) or not _DAY.fullmatch(value.strip()):
        return ""
    return value.strip()


def _record_of(item: object, turn: object, by_user: bool) -> Record | None:
    """One record from one reply item, or None when it names no thing."""
    if not isinstance(item, Mapping):
        return None
    kind = canonical_kind(_text_field(item, "kind", MAX_KIND_CHARS))
    thing = canonical(_text_field(item, "thing", MAX_THING_CHARS))
    if not kind or not thing:
        return None
    stated = _stated_day(item)
    return Record(
        kind=kind,
        thing=thing,
        event=canonical(_text_field(item, "event", MAX_EVENT_CHARS)),
        day=stated or day_of_path(turn.source_path),
        quantity=_quantity_field(item),
        by_user=by_user,
        dated=bool(stated),
        source_path=turn.source_path,
        source_sha256=getattr(turn, "source_sha256", ""),
        byte_start=turn.byte_start,
        byte_end=turn.byte_end,
        span_sha256=turn.span_sha256,
    )


def _record_items(value: object) -> list[object]:
    """The `records` list of one turn's reply value; a list-shaped reply carries none."""
    if not isinstance(value, Mapping):
        return []
    items = value.get("records")
    if not isinstance(items, list):
        return []
    return items[:MAX_RECORDS_PER_TURN]


def _posted_once(records: Iterable[Record | None]) -> list[Record]:
    kept = {record.posting_key: record for record in records if record is not None}
    return list(kept.values())


def records_of(turn: object, value: object, by_user: bool = True) -> list[Record]:
    """The records a reply wrote for one turn, each once, with a day."""
    found = (_record_of(item, turn, by_user) for item in _record_items(value))
    return [record for record in _posted_once(found) if record.day]


# --- the store ---------------------------------------------------------------


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.executescript(LEDGER_DDL)


def has_ledger(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (LEDGER_TABLE,)
    ).fetchone()
    return row is not None


def post(connection: sqlite3.Connection, records: Iterable[Record]) -> int:
    """Post each record once; how many were new."""
    before = connection.total_changes
    connection.executemany(_INSERT, [record.row() for record in records])
    connection.commit()
    return connection.total_changes - before


def rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    """Every posted row, in posting-key order, for the generation build to copy."""
    if not has_ledger(connection):
        return []
    return [tuple(row) for row in connection.execute(_SELECT + " ORDER BY posting_key")]


def rows_in_store(path: Path) -> list[tuple[object, ...]]:
    """The rows of a fact-keys store on disk, or none when there is no store."""
    if not Path(path).exists():
        return []
    connection = sqlite3.connect(str(path))
    try:
        return rows(connection)
    finally:
        connection.close()


def write_table(connection: sqlite3.Connection, table_rows: Sequence[tuple[object, ...]]) -> None:
    """The generation's copy: the table exists even when the night posted nothing."""
    ensure_table(connection)
    connection.executemany(_INSERT, list(table_rows))
    connection.commit()


def _record_from_row(row: Sequence[object]) -> Record:
    fields = dict(zip(COLUMNS[1:], row[1:]))
    quantity = fields["quantity"]
    return Record(
        kind=str(fields["kind"]),
        thing=str(fields["thing"]),
        event=str(fields["event"]),
        day=str(fields["day"]),
        quantity=None if quantity is None else float(quantity),
        by_user=bool(fields["by_user"]),
        dated=bool(fields["dated"]),
        source_path=str(fields["source_path"]),
        source_sha256=str(fields["source_sha256"]),
        byte_start=int(fields["byte_start"]),
        byte_end=int(fields["byte_end"]),
        span_sha256=str(fields["span_sha256"]),
    )


def _records_of_kind(
    connection: sqlite3.Connection, kind: str, window: tuple[str, str] | None
) -> list[Record]:
    if window is None:
        query = connection.execute(_SELECT + " WHERE kind = ? ORDER BY day, posting_key", (kind,))
    else:
        query = connection.execute(
            _SELECT + " WHERE kind = ? AND day BETWEEN ? AND ? ORDER BY day, posting_key",
            (kind, window[0], window[1]),
        )
    return [_record_from_row(row) for row in query]


# --- same event: Fellegi–Sunter over the fields ----------------------------


def _weight(field: str, agrees: bool) -> float:
    m, u = _FIELD_ODDS[field]
    if agrees:
        return math.log(m / u)
    return math.log((1.0 - m) / (1.0 - u))


def _tokens(text: str) -> frozenset[str]:
    return frozenset(text.split())


def thing_agrees(a: str, b: str) -> bool:
    """The two names overlap enough to be one thing: "road bike" and "my road bike"."""
    first, second = _tokens(a), _tokens(b)
    if not first or not second:
        return False
    if first <= second or second <= first:
        return True
    return len(first & second) / len(first | second) >= THING_OVERLAP


def _event_agrees(a: str, b: str) -> bool:
    return a == b or not a or not b


def _quantity_agrees(a: float | None, b: float | None) -> bool:
    return a is None or b is None or a == b


def _days_apart(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def match_score(a: Record, b: Record) -> float:
    """The summed field weights of the pair; the threshold decides."""
    return _weight("thing", thing_agrees(a.thing, b.thing)) + _weight(
        "event", _event_agrees(a.event, b.event)
    )


def _new_event_evidence(a: Record, b: Record) -> bool:
    """Outside the window, or two different stated quantities: a new event, not a repeat."""
    if a.kind != b.kind or _days_apart(a.day, b.day) > SAME_EVENT_DAYS:
        return True
    return not _quantity_agrees(a.quantity, b.quantity)


def same_event(a: Record, b: Record) -> bool:
    """Within the window, no evidence of a new event, and the fields agree: one event, posted twice."""
    if _new_event_evidence(a, b):
        return False
    return match_score(a, b) >= MATCH_THRESHOLD


def _touches(item: object, group: Sequence[object], together: Callable) -> bool:
    return any(together(item, other) for other in group)


def _split_by_touch(
    item: object, groups: list[list[object]], together: Callable
) -> tuple[list[list[object]], list[list[object]]]:
    """(the groups the item touches, the groups it does not)."""
    touched = [group for group in groups if _touches(item, group, together)]
    apart = [group for group in groups if group not in touched]
    return touched, apart


def _flattened(groups: Sequence[Sequence[object]]) -> list[object]:
    return [member for group in groups for member in group]


def _groups(items: Sequence[object], together: Callable) -> list[list[object]]:
    """Transitive grouping under a symmetric pair test; small sets, no union-find needed."""
    groups: list[list[object]] = []
    for item in items:
        touched, groups = _split_by_touch(item, groups, together)
        groups.append([item] + _flattened(touched))
    return groups


def _tier(records: Sequence[Record]) -> str:
    if records and all(record.by_user and record.dated for record in records):
        return CONFIRMED
    return PROBABLE


def _pointer(record: Record) -> Pointer:
    return Pointer(
        record.day,
        record.event,
        record.source_path,
        record.source_sha256,
        record.byte_start,
        record.byte_end,
        record.span_sha256,
    )


def _things_agree(a: Record, b: Record) -> bool:
    return thing_agrees(a.thing, b.thing)


def count(
    connection: sqlite3.Connection, kind: str, window: tuple[str, str] | None = None
) -> LedgerCount | None:
    """The count for one kind, or None when the generation carries no ledger at all.

    `kind` is the head noun the question counts (`aggregation_pass.counted_kind`);
    `window` is a pair of ISO days, inclusive, or None for all time. `events` merges
    records under the same-event rule, `things` merges by name alone: "how many
    bikes did I service" is answered by things, "how many times" by events.
    """
    if not has_ledger(connection):
        return None
    records = _records_of_kind(connection, canonical_kind(kind), window)
    return LedgerCount(
        kind=canonical_kind(kind),
        window=window,
        records=len(records),
        events=len(_groups(records, same_event)),
        things=len(_groups(records, _things_agree)),
        tier=_tier(records),
        pointers=tuple(_pointer(record) for record in records),
    )


def sum_quantities(
    connection: sqlite3.Connection, kind: str, window: tuple[str, str] | None = None
) -> float | None:
    """The stated quantities of one kind, one per event, added; None without a ledger."""
    if not has_ledger(connection):
        return None
    records = _records_of_kind(connection, canonical_kind(kind), window)
    quantities = (_event_quantity(group) for group in _groups(records, same_event))
    return float(sum(value for value in quantities if value is not None))


def _event_quantity(group: Sequence[Record]) -> float | None:
    stated = [record.quantity for record in group if record.quantity is not None]
    if not stated:
        return None
    return max(stated)


def reconcile(stated: float | None, counted: LedgerCount | None) -> Reconciliation:
    """The reader's number against the ledger's; a mismatch is flagged, not resolved."""
    unit = None
    if stated is not None and counted is not None:
        unit = _matching_unit(stated, counted)
    return Reconciliation(stated, counted, unit)


def _matching_unit(stated: float, counted: LedgerCount) -> str | None:
    if stated == counted.things:
        return "things"
    if stated == counted.events:
        return "events"
    return None


# --- the reader's one call ---------------------------------------------------


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


# --- the recurrence gate -----------------------------------------------------


def _by_kind(records: Iterable[Record]) -> dict[str, list[Record]]:
    grouped: dict[str, list[Record]] = {}
    for record in records:
        grouped.setdefault(record.kind, []).append(record)
    return grouped


def _all_records(connection: sqlite3.Connection) -> list[Record]:
    if not has_ledger(connection):
        return []
    return [_record_from_row(row) for row in connection.execute(_SELECT + " ORDER BY kind, day")]


def _things_of_every_kind(records: Iterable[Record]) -> list[Recurrence]:
    by_kind = _by_kind(records)
    found: list[Recurrence] = []
    for kind in sorted(by_kind):
        found.extend(_recurrence(group) for group in _groups(by_kind[kind], _things_agree))
    return found


def recurring(connection: sqlite3.Connection, minimum_days: int = 2) -> list[Recurrence]:
    """Every thing with records on `minimum_days` or more distinct days, by kind then name."""
    found = _things_of_every_kind(_all_records(connection))
    return [item for item in found if len(item.days) >= minimum_days]


def _recurrence(group: Sequence[Record]) -> Recurrence:
    ordered = sorted(group, key=lambda record: (record.day, record.posting_key))
    days = tuple(dict.fromkeys(record.day for record in ordered))
    return Recurrence(ordered[0].kind, ordered[0].thing, days, tuple(_pointer(r) for r in ordered))


def entity_slug(recurrence: Recurrence) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", f"{recurrence.kind} {recurrence.thing}".casefold())
    return "ledger-" + words.strip("-")[:80]


def pointer_line(pointer: Pointer) -> str:
    """One dated line, code-written, pointing at the source bytes."""
    what = pointer.event or "mentioned"
    return f"- {pointer.day} — {what} — {pointer.source_path} bytes {pointer.byte_start}-{pointer.byte_end}"


def _new_page(recurrence: Recurrence, today: str) -> str:
    return (
        "---\n"
        "type: entity\n"
        "status: active\n"
        "confidence: medium\n"
        "source_authority: ai-derived\n"
        f"created: {today}\n"
        "---\n"
        f"# {recurrence.thing} ({recurrence.kind})\n\n"
        f"One-sentence summary: a {recurrence.kind} the ledger has seen on more than one day; "
        "the dated lines below point at the turns it came from and are extended by code.\n\n"
        f"{LEDGER_SECTION}\n"
    )


def _page_with_section(existing: bytes | None, recurrence: Recurrence, today: str) -> str:
    """The page as it is, or a new one, with its ledger section in place."""
    if existing is None:
        return _new_page(recurrence, today)
    text = existing.decode("utf-8")
    if LEDGER_SECTION in text:
        return text
    return text.rstrip("\n") + f"\n\n{LEDGER_SECTION}\n"


def _missing_lines(text: str, recurrence: Recurrence) -> list[str]:
    lines = dict.fromkeys(pointer_line(pointer) for pointer in recurrence.pointers)
    return [line for line in lines if line not in text]


def entity_page_bytes(existing: bytes | None, recurrence: Recurrence, today: str) -> bytes | None:
    """The page with every pointer line it lacks appended; None when nothing is new."""
    text = _page_with_section(existing, recurrence, today)
    missing = _missing_lines(text, recurrence)
    if not missing:
        return None
    return (text.rstrip("\n") + "\n" + "\n".join(missing) + "\n").encode("utf-8")


def _existing_page(page: Path) -> bytes | None:
    from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES, read_stable_bytes

    if not page.exists():
        return None
    return read_stable_bytes(page, MAX_KNOWLEDGE_PAGE_BYTES, label="ledger entity page")


def _page_change(vault: Path, recurrence: Recurrence, today: str):
    """(operation id, change, precondition) for one page, or None when it is current."""
    from markdown_transaction import stable_operation_id
    from reliable_memory import sha256_bytes

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
