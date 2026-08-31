"""A bitemporal reading of frozen claim/v1 records.

Two clocks, both already carried by `claim/v1` and neither invented here:

* **valid time** — when the fact was true in the world. `validity.from` /
  `validity.to`, half-open `[from, to)`, already order-checked by
  `claims._require_ordered_interval`. A claim that declares none is read as
  valid from its own observation.
* **transaction time** — when this vault learned it. `observed_at`, which
  `claims._require_observation` binds to the exact daily block the evidence was
  cited from, so it is proved by evidence rather than asserted by a writer.

The third thing a bitemporal store needs is the *end* of the transaction-time
interval — Graphiti's `expired_at`, with `invalid_at` as its valid-time twin.
It is not stored, because storing it would mean mutating an existing claim when
a later one arrives, and in this vault claims are immutable evidence. It is
derived here, deterministically, from the ledger itself. Like
`cache/claims.sqlite3`, this reading is disposable; the Markdown ledger stays
canonical.

The conflict decision carries no LLM. Two active claims conflict when they
share the normalised `(subject, relation, qualifiers)` key and their canonical
`value` bytes differ — and only when the relation is single-valued, because a
later `member-of` does not contradict an earlier one.

Design, sources and the refusals: `docs/research/2026-08-28-bitemporal-claims.md`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from claims import (
    RELATIONS,
    IndexedClaim,
    NormalizedClaim,
    _canonical_time,
    _instant_of,
)
from reliable_memory import canonical_json_bytes

# A relation is single-valued when a subject can hold exactly one value for it
# at one instant. Only those take part in automatic invalidation.
SINGLE_VALUED_RELATIONS = frozenset(
    {"equals", "has-state", "has-value", "located-at", "starts-at", "ends-at"}
)
# Named so the partition is visible, not inferred. A relation in neither set
# is read as multi-valued: it never invalidates a sibling, which loses no fact.
MULTI_VALUED_RELATIONS = frozenset({"member-of", "uses", "depends-on"})

_REQUIRED_FIELD_SET = frozenset(
    {"subject", "relation", "value", "qualifiers", "observed_at", "validity"}
)


class BitemporalRefusal(ValueError):
    """A bitemporal reading refused by name rather than guess an order."""


@dataclass(frozen=True)
class Belief:
    """One claim read on both clocks, with its derived ends."""

    record: Mapping[str, object]
    page: str | None
    valid_from: str
    valid_to: str | None
    observed_at: str
    expired_at: str | None = None
    invalid_at: str | None = None

    @property
    def effective_valid_to(self) -> str | None:
        """Where belief in this claim actually ends, open when neither end is set."""
        return _earliest(self.valid_to, self.invalid_at)

    @property
    def claim_id(self) -> str:
        return str(self.record.get("id", ""))


def as_of(
    records: Sequence[object], *, valid_at: object, known_at: object | None = None
) -> tuple[Belief, ...]:
    """Every claim believed true at `valid_at`, using only what `known_at` knew.

    `valid_at` asks about the world; `known_at` asks about this vault. Leaving
    `known_at` open uses everything the vault knows now.
    """
    moment = _instant(valid_at, label="valid_at")
    believed = history(records, known_at=known_at)
    return tuple(item for item in believed if _contains(item, moment))


def history(
    records: Sequence[object], *, known_at: object | None = None
) -> tuple[Belief, ...]:
    """Every visible claim with the transaction- and valid-time ends it earned."""
    visible = _visible(_beliefs(records), known_at)
    resolved: list[Belief] = []
    for group in _grouped(visible).values():
        resolved.extend(_resolved_group(group))
    return tuple(sorted(resolved, key=_belief_order))


def index_as_of(
    index: object,
    *,
    valid_at: object,
    known_at: object | None = None,
    subject: str | None = None,
) -> tuple[Belief, ...]:
    """The same question asked of a built claim index rather than loose records."""
    records = index.active_records(subject=subject)
    return as_of(records, valid_at=valid_at, known_at=known_at)


def is_single_valued(relation: object) -> bool:
    """Whether one later value for this relation contradicts an earlier one."""
    return str(relation).strip().casefold() in SINGLE_VALUED_RELATIONS


def unclassified_relations() -> frozenset[str]:
    """Relations the partition does not name; read as multi-valued until it does."""
    return frozenset(RELATIONS) - SINGLE_VALUED_RELATIONS - MULTI_VALUED_RELATIONS


def _beliefs(records: Sequence[object]) -> list[Belief]:
    _require_sequence(records)
    return [_as_belief(item) for item in records]


def _require_sequence(records: object) -> None:
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise BitemporalRefusal(
            "bitemporal_records_invalid: claims must be given as a sequence"
        )


def _as_belief(item: object) -> Belief:
    if isinstance(item, IndexedClaim):
        return _belief(item.claim.record, item.page)
    if isinstance(item, NormalizedClaim):
        return _belief(item.record, None)
    return _belief(item, None)


def _belief(record: object, page: str | None) -> Belief:
    _require_record(record)
    assert isinstance(record, Mapping)
    validity = record["validity"]
    assert isinstance(validity, Mapping)
    observed_at = _instant_text(record["observed_at"], label="observed_at")
    valid_to = _optional_instant_text(validity["to"], label="validity to")
    valid_from = (
        _optional_instant_text(validity["from"], label="validity from") or observed_at
    )
    return Belief(record, page, valid_from, valid_to, observed_at)


def _require_record(record: object) -> None:
    _require_bitemporal_fields(record)
    assert isinstance(record, Mapping)
    _require_validity_shape(record.get("validity"))


def _require_bitemporal_fields(record: object) -> None:
    if not isinstance(record, Mapping):
        raise BitemporalRefusal(
            "bitemporal_record_invalid: a claim record must be a mapping"
        )
    if not _REQUIRED_FIELD_SET.issubset(record):
        raise BitemporalRefusal(
            "bitemporal_record_invalid: a claim record is missing bitemporal fields"
        )


def _require_validity_shape(validity: object) -> None:
    if not isinstance(validity, Mapping):
        raise BitemporalRefusal(
            "bitemporal_validity_invalid: claim validity must be a mapping"
        )
    if set(validity) != {"from", "to"}:
        raise BitemporalRefusal(
            "bitemporal_validity_invalid: claim validity must be exactly from and to"
        )


def _instant_text(value: object, *, label: str) -> str:
    """Canonical text, refusing by name rather than coercing an unreadable time."""
    try:
        canonical = _canonical_time(value, nullable=False, label=label)
    except (TypeError, ValueError) as exc:
        raise BitemporalRefusal(f"bitemporal_time_invalid: {exc}") from exc
    assert isinstance(canonical, str)
    return canonical


def _optional_instant_text(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _instant_text(value, label=label)


def _instant(value: object, *, label: str) -> datetime:
    return _instant_of(_instant_text(value, label=label))


def _earliest(first: str | None, second: str | None) -> str | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second, key=_instant_of)


def _contains(item: Belief, moment: datetime) -> bool:
    """Half-open: the start is included, the end is not."""
    if _instant_of(item.valid_from) > moment:
        return False
    end = item.effective_valid_to
    if end is None:
        return True
    return _instant_of(end) > moment


def _visible(beliefs: Sequence[Belief], known_at: object | None) -> list[Belief]:
    """Active claims this vault had already observed at `known_at`."""
    active = _active(beliefs)
    if known_at is None:
        return active
    return _observed_by(active, _instant(known_at, label="known_at"))


def _active(beliefs: Sequence[Belief]) -> list[Belief]:
    return [item for item in beliefs if item.record.get("lifecycle") == "active"]


def _observed_by(beliefs: Sequence[Belief], limit: datetime) -> list[Belief]:
    return [item for item in beliefs if _instant_of(item.observed_at) <= limit]


def _grouped(beliefs: Sequence[Belief]) -> dict[bytes, list[Belief]]:
    groups: dict[bytes, list[Belief]] = {}
    for item in beliefs:
        groups.setdefault(_key(item.record), []).append(item)
    return groups


def _key(record: Mapping[str, object]) -> bytes:
    """The bitemporal key: everything about the fact except what it values."""
    return canonical_json_bytes(
        {
            "subject": record["subject"],
            "relation": record["relation"],
            "qualifiers": record["qualifiers"],
        }
    )


def _value_bytes(record: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(record["value"])


def _belief_order(item: Belief) -> tuple[datetime, str]:
    return (_instant_of(item.observed_at), item.claim_id)


def _resolved_group(group: list[Belief]) -> list[Belief]:
    ordered = sorted(group, key=_belief_order)
    if not is_single_valued(ordered[0].record["relation"]):
        return ordered
    _require_orderable(ordered)
    return [_expired(ordered, index) for index in range(len(ordered))]


def _expired(ordered: list[Belief], index: int) -> Belief:
    successor = _successor(ordered, index)
    if successor is None:
        return ordered[index]
    return replace(
        ordered[index],
        expired_at=successor.observed_at,
        invalid_at=successor.valid_from,
    )


def _successor(ordered: list[Belief], index: int) -> Belief | None:
    """The first later claim that says something else about the same fact."""
    value = _value_bytes(ordered[index].record)
    for candidate in ordered[index + 1 :]:
        if _value_bytes(candidate.record) != value:
            return candidate
    return None


def _require_orderable(ordered: list[Belief]) -> None:
    for first, second in zip(ordered, ordered[1:]):
        _require_distinct_observation(first, second)


def _require_distinct_observation(first: Belief, second: Belief) -> None:
    """Conflicting claims observed at one instant carry no order to read."""
    if _instant_of(first.observed_at) != _instant_of(second.observed_at):
        return
    if _value_bytes(first.record) == _value_bytes(second.record):
        return
    raise BitemporalRefusal(
        "bitemporal_ambiguous_observation: conflicting claims "
        f"{first.claim_id!r} and {second.claim_id!r} share observation "
        f"{first.observed_at}; the evidence carries no order between them"
    )
