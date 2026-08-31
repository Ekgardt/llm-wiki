"""Evidence-verified atomic claims and their rebuildable derived index."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bounded_io import read_stable_bytes
from compile_cache import _restrict_owner_only, _verify_owner_only
from evidence_resolver import (
    EvidenceRef,
    EvidenceResolutionError,
    EvidenceResolver,
    daily_entries,
)
from reliable_memory import (
    canonical_json_bytes,
    open_operational_db,
    sha256_bytes,
    validate_schema,
    validate_state_root,
)

SCHEMA_DIR = Path(__file__).with_name("schemas")
LEDGER_SCHEMA = SCHEMA_DIR / "claim-ledger-v1.json"
CANDIDATE_SCHEMA = SCHEMA_DIR / "claim-candidate-v1.json"
RELATION_SCHEMA = SCHEMA_DIR / "claim-relations-v1.json"
MAX_CLAIM_PAGE_BYTES = 4 * 1024 * 1024
MAX_CANDIDATES = 50
MAX_ACTIVE_RECORDS = 10_000
MAX_DECIMAL_CHARS = 128
MAX_DECIMAL_DIGITS = 128
MAX_DECIMAL_EXPONENT = 128
CLAIM_INDEX_SCHEMA_VERSION = "claim-index/v2"
RELATIONS = frozenset(
    {
        "equals",
        "has-state",
        "has-value",
        "member-of",
        "located-at",
        "starts-at",
        "ends-at",
        "uses",
        "depends-on",
    }
)
_NON_SUBSTANTIVE_RELATIONS = frozenset(
    {"title", "summary", "link", "links", "provenance", "mention", "mentions"}
)
# Two anchored heading forms, and only these. The bare form was the original
# contract; the titled form is what `daily_log_append` has always written, so
# every daily log this vault holds — back to 2026-04-13 — is titled. The reader
# was written against a shape that never existed here (NEW-120), and widening
# the writer instead would leave the whole append-only history unreadable. Both
# forms anchor on the start of the line and end it with the date, so nothing
# ambiguous is admitted; anything else still refuses by name. See
# `docs/research/2026-08-28-which-daily-header-is-canonical.md`.
_DATE_RE = re.compile(r"^# (?:[^\r\n]*?[ \t]\u2014[ \t])?(\d{4}-\d{2}-\d{2})(?:\r?\n|$)")
_BLOCK_RE = re.compile(rb"(?m)^## \[(\d{2}:\d{2}:\d{2})\][^\r\n]*(?:\r?\n|$)")
_CLAIMS_RE = re.compile(
    r"(?ms)^## Claims[ \t]*\r?\n```json[ \t]*\r?\n([^\r\n]+)\r?\n```[ \t]*(?=\r?\n(?:## |\Z)|\Z)"
)
_EXTRACTION_FIELDS = {
    "id",
    "text",
    "subject",
    "relation",
    "value",
    "qualifiers",
    "validity",
    "lifecycle",
    "confidence",
    "authority",
    "evidence",
    "links",
    "extractor_version",
}
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_DECIMAL_INPUT_RE = re.compile(
    r"^[+-]?(?:(?P<integer>\d+)(?:\.(?P<fraction>\d*))?|"
    r"\.(?P<leading_fraction>\d+))(?:[eE](?P<exponent>[+-]?\d+))?$"
)


class EvidenceMismatch(ValueError):
    """A proposed claim is not bound to the exact referenced literal bytes."""


@dataclass(frozen=True)
class TimestampBlock:
    source: bytes
    source_sha256: str
    daily_id: str
    block_id: str
    observed_at: str
    byte_start: int
    byte_end: int
    bytes: bytes


@dataclass(frozen=True)
class Claim:
    record: dict[str, object]
    block: TimestampBlock


@dataclass(frozen=True)
class VerifiedClaim:
    claim: Claim
    evidence_bytes: bytes

    @property
    def record(self) -> dict[str, object]:
        return self.claim.record


@dataclass(frozen=True)
class NormalizedClaim:
    record: dict[str, object]


@dataclass(frozen=True)
class IndexedClaim:
    page: str
    claim: NormalizedClaim
    ledger_backed: bool = True


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _trim(value: object, *, label: str, fold: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = _folded(_nfc(" ".join(value.split())), fold)
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _folded(text: str, fold: bool) -> str:
    if fold:
        return text.casefold()
    return text


def _canonical_time(value: object, *, nullable: bool, label: str) -> str | None:
    if value is None and nullable:
        return None
    text = _trim(value, label=label)
    if "T" not in text:
        return _canonical_date_text(text, label=label)
    return _canonical_timestamp_text(text, label=label)


def _canonical_date_text(text: str, *, label: str) -> str:
    try:
        if date.fromisoformat(text).isoformat() != text:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC date or timestamp") from exc
    return text


# `datetime.fromisoformat` accepted exactly three or six fractional digits until
# Python 3.11; this project supports 3.10, where `…:00.5Z` — a perfectly ordinary
# half second — raises `Invalid isoformat string`. Verified on 3.10.20 against
# 3.12.3 on 2026-08-30, and it is why `test_a_sub_second_validity_interval_is_
# not_refused_as_inverted` failed on every 3.10 job in CI while passing locally.
# Padding to six digits is exact: it changes no instant and no rendered output,
# because `isoformat()` prints six digits on both versions either way.
_FRACTIONAL_SECONDS_RE = re.compile(
    r"^(?P<head>.*T\d{2}:\d{2}:\d{2})\.(?P<digits>\d+)(?P<tail>.*)$"
)


def _six_digit_fraction(text: str) -> str:
    """The same instant with a fraction every supported Python can read."""
    match = _FRACTIONAL_SECONDS_RE.match(text)
    if match is None:
        return text
    digits = match.group("digits")
    if len(digits) > 6:
        return text
    return f"{match.group('head')}.{digits.ljust(6, '0')}{match.group('tail')}"


def _canonical_timestamp_text(text: str, *, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(
            _six_digit_fraction(text[:-1] + "+00:00" if text.endswith("Z") else text)
        )
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC date or timestamp") from exc
    return parsed.isoformat().replace("+00:00", "Z").replace(".000000Z", "Z")


def _strict_rfc3339_utc(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a strict UTC RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(_six_digit_fraction(value[:-1] + "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not a real RFC3339 date/time") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must be UTC")
    return value


def _canonical_decimal(value: object) -> str:
    source = _decimal_source(value)
    _require_decimal_bounds(_decimal_match(source))
    return _canonical_decimal_text(source)


def _require_decimal_type(value: object) -> None:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("number value must be a decimal string, integer, or Decimal")


def _decimal_source(value: object) -> str:
    _require_decimal_type(value)
    if not isinstance(value, str):
        return str(value)
    if value != value.strip() or not value:
        raise ValueError("number value is invalid")
    return value


def _decimal_match(source: str) -> re.Match[str]:
    if len(source) > MAX_DECIMAL_CHARS:
        raise ValueError("number value exceeds the input length limit")
    match = _DECIMAL_INPUT_RE.fullmatch(source)
    if match is None:
        raise ValueError("number value is invalid")
    return match


def _require_decimal_bounds(match: re.Match[str]) -> None:
    integer = match["integer"] or ""
    coefficient_digits = len(integer) + len(_decimal_fraction(match))
    if coefficient_digits > MAX_DECIMAL_DIGITS:
        raise ValueError("number value exceeds the coefficient digit limit")
    position = len(integer) + _decimal_exponent(match)
    if _expanded_digits(position, coefficient_digits) > MAX_DECIMAL_DIGITS:
        raise ValueError("number value exceeds the expanded digit limit")


def _decimal_fraction(match: re.Match[str]) -> str:
    fraction = match["fraction"]
    if fraction is None:
        return match["leading_fraction"] or ""
    return fraction


def _decimal_exponent(match: re.Match[str]) -> int:
    text = match["exponent"] or "0"
    if len(text.lstrip("+-")) > 3:
        raise ValueError("number value exponent is out of range")
    exponent = int(text)
    if abs(exponent) > MAX_DECIMAL_EXPONENT:
        raise ValueError("number value exponent is out of range")
    return exponent


def _expanded_digits(decimal_position: int, coefficient_digits: int) -> int:
    """How many digits the value would need written out in full."""
    if decimal_position <= 0:
        return 1 - decimal_position + coefficient_digits
    if decimal_position >= coefficient_digits:
        return decimal_position
    return coefficient_digits


def _trimmed_decimal(result: str) -> str:
    if "." in result:
        return result.rstrip("0").rstrip(".")
    return result


def _canonical_decimal_text(source: str) -> str:
    result = _trimmed_decimal(format(_finite_decimal(source), "f"))
    if len(result) > MAX_DECIMAL_CHARS:
        raise ValueError("number value exceeds the canonical length limit")
    if Decimal(result).is_zero():
        return "0"
    return result


def _finite_decimal(source: str) -> Decimal:
    try:
        number = Decimal(source)
    except InvalidOperation as exc:
        raise ValueError("number value is invalid") from exc
    if not number.is_finite():
        raise ValueError("number value must be finite")
    return number


def _normalize_value(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
        raise ValueError("claim value must be typed")
    kind = value["type"]
    _require_value_fields(value, kind)
    raw = value["value"]
    if kind == "number":
        return {
            "type": "number",
            "value": _canonical_decimal(raw),
            "unit": _trim(value["unit"], label="number unit", fold=True),
        }
    return {"type": kind, "value": _normalized_scalar(kind, raw)}


def _require_value_fields(value: Mapping[str, object], kind: str) -> None:
    expected = {"type", "value", "unit"} if kind == "number" else {"type", "value"}
    if set(value) != expected:
        raise ValueError("typed claim value fields are invalid")


def _normalized_scalar(kind: str, raw: object) -> object:
    normalizer = _SCALAR_NORMALIZERS.get(kind)
    if normalizer is None:
        raise ValueError("claim value type is invalid")
    return normalizer(raw)


def _normalized_string(raw: object) -> object:
    if raw == "":
        return ""
    return _trim(raw, label="string value")


def _normalized_entity(raw: object) -> object:
    return _trim(raw, label="entity value", fold=True)


def _normalized_boolean(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise ValueError("boolean value is invalid")
    return raw


def _normalized_date(raw: object) -> object:
    result = _canonical_time(raw, nullable=False, label="date value")
    if "T" in str(result):
        raise ValueError("date value must not contain a time")
    return result


def _normalized_timestamp(raw: object) -> object:
    result = _canonical_time(raw, nullable=False, label="timestamp value")
    if "T" not in str(result):
        raise ValueError("timestamp value must contain a time")
    return result


_SCALAR_NORMALIZERS = {
    "string": _normalized_string,
    "entity": _normalized_entity,
    "boolean": _normalized_boolean,
    "date": _normalized_date,
    "timestamp": _normalized_timestamp,
}


def _validate_interval(validity: object) -> dict[str, str | None]:
    if not isinstance(validity, Mapping) or set(validity) != {"from", "to"}:
        raise ValueError("claim validity must contain exactly from and to")
    start = _canonical_time(validity["from"], nullable=True, label="validity from")
    end = _canonical_time(validity["to"], nullable=True, label="validity to")
    _require_ordered_interval(start, end)
    return {"from": start, "to": end}


def _require_ordered_interval(start: str | None, end: str | None) -> None:
    if start is None or end is None:
        return
    if _instant_of(start) >= _instant_of(end):
        raise ValueError("claim validity must be a non-empty half-open interval")


def _instant_of(value: str) -> datetime:
    """A bare date is its first instant; canonical times order by instant, not text.

    Text ordering is wrong here because canonical form keeps fractional seconds
    and `.` sorts before `Z`, so `…:00.5Z` compares as earlier than `…:00Z`.
    """
    if "T" in value:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    return datetime.fromisoformat(f"{value}T00:00:00+00:00")


def _semantic_payload(record: Mapping[str, object]) -> dict[str, object]:
    qualifiers = [
        {
            "key": _trim(item["key"], label="qualifier key", fold=True),
            "value": _normalize_value(item["value"]),
        }
        for item in record["qualifiers"]
    ]
    qualifiers.sort(key=canonical_json_bytes)
    return {
        "subject": _trim(record["subject"], label="subject", fold=True),
        "relation": _trim(record["relation"], label="relation", fold=True),
        "value": _normalize_value(record["value"]),
        "qualifiers": qualifiers,
        "validity": _validate_interval(record["validity"]),
    }


def _validate_extracted_record(record: object) -> None:
    if not isinstance(record, dict) or set(record) != _EXTRACTION_FIELDS:
        raise ValueError("claim extraction schema fields are invalid")
    for field in ("id", "text", "subject", "relation", "extractor_version"):
        _trim(record[field], label=field)
    _require_extracted_relation(record["relation"])
    _normalize_value(record["value"])
    _require_extracted_qualifiers(record["qualifiers"])
    _validate_interval(record["validity"])
    _require_extracted_enums(record)
    _require_extracted_evidence(record["evidence"])
    _require_extracted_links(record["links"])


def _require_extracted_relation(relation: object) -> None:
    if _trim(relation, label="relation", fold=True) not in RELATIONS:
        raise ValueError("claim extraction schema relation is invalid")


def _require_extracted_qualifiers(qualifiers: object) -> None:
    if not isinstance(qualifiers, list) or len(qualifiers) > 100:
        raise ValueError("claim extraction schema qualifiers are invalid")
    for qualifier in qualifiers:
        _require_extracted_qualifier(qualifier)


def _require_extracted_qualifier(qualifier: object) -> None:
    if not isinstance(qualifier, dict) or set(qualifier) != {"key", "value"}:
        raise ValueError("claim extraction schema qualifier fields are invalid")
    _trim(qualifier["key"], label="qualifier key")
    _normalize_value(qualifier["value"])


_EXTRACTION_ENUMS = (
    ("lifecycle", frozenset({"active", "superseded", "inactive", "quarantined"})),
    ("confidence", frozenset({"high", "medium", "low"})),
    ("authority", frozenset({"user", "web", "ai-derived", "inferred"})),
)


def _require_extracted_enums(record: Mapping[str, object]) -> None:
    for field, allowed in _EXTRACTION_ENUMS:
        if record[field] not in allowed:
            raise ValueError(f"claim extraction schema {field} is invalid")


def _require_extracted_evidence(evidence: object) -> None:
    if not isinstance(evidence, dict) or set(evidence) != {
        "reference",
        "sha256",
        "text",
    }:
        raise ValueError("claim extraction schema evidence fields are invalid")
    EvidenceRef.parse(evidence["reference"])
    _require_extracted_hash(evidence["sha256"])
    _trim(evidence["text"], label="evidence text")


def _require_extracted_hash(digest: object) -> None:
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("claim extraction schema evidence hash is invalid")


def _require_extracted_links(links: object) -> None:
    if not isinstance(links, list) or len(links) > 100:
        raise ValueError("claim extraction schema links are invalid")
    if any(_is_empty_link(item) for item in links):
        raise ValueError("claim extraction schema links are invalid")


def _is_empty_link(item: object) -> bool:
    return not isinstance(item, str) or not item


def _timestamp_block(
    source: bytes, digest: str, daily_id: str, entry: tuple[str, int, int]
) -> TimestampBlock:
    block_id, start, end = entry
    _require_block_time(block_id)
    return TimestampBlock(
        source,
        digest,
        daily_id,
        block_id,
        f"{daily_id}T{block_id}Z",
        start,
        end,
        source[start:end],
    )


def _require_block_time(block_id: str) -> None:
    """A claim block is named by the time it happened at, and nothing else."""
    try:
        datetime.strptime(block_id, "%H:%M:%S")
    except ValueError as exc:
        raise ValueError("claim block timestamp is invalid") from exc


class ClaimPipeline:
    def __init__(self, resolver: EvidenceResolver):
        if not isinstance(resolver, EvidenceResolver):
            raise TypeError("resolver must be an EvidenceResolver")
        self.resolver = resolver
        self.calls: list[str] = []

    def split_blocks(self, source: bytes) -> tuple[TimestampBlock, ...]:
        self.calls.append("split_blocks")
        if not isinstance(source, bytes):
            raise TypeError("source must be immutable bytes")
        daily_id = _claim_source_daily_id(source)
        digest = sha256_bytes(source)
        return tuple(
            _timestamp_block(source, digest, daily_id, entry)
            for entry in daily_entries(source)
        )

    def extract(
        self, block: TimestampBlock, result: Mapping[str, object]
    ) -> tuple[Claim, ...]:
        self.calls.append("extract")
        if not isinstance(block, TimestampBlock) or not isinstance(result, Mapping):
            raise TypeError("claim extraction inputs are invalid")
        records = _extraction_records(result)
        return tuple(Claim(_validated_record(raw), block) for raw in records)

    def verify_literal(
        self, claim: Claim, reference: EvidenceRef | None = None
    ) -> VerifiedClaim:
        self.calls.append("verify_evidence")
        if not isinstance(claim, Claim):
            raise TypeError("claim must be extracted before evidence verification")
        evidence = claim.record["evidence"]
        assert isinstance(evidence, dict)
        resolved = self._resolved_evidence(claim, evidence, reference)
        _require_verified_literal(claim, evidence, resolved)
        return VerifiedClaim(claim, resolved.bytes)

    def _resolved_evidence(
        self,
        claim: Claim,
        evidence: Mapping[str, object],
        reference: EvidenceRef | None,
    ) -> object:
        try:
            embedded = EvidenceRef.parse(evidence["reference"])
            _require_matching_reference(embedded, claim, reference)
            return self.resolver.resolve(embedded)
        except EvidenceMismatch:
            raise
        except (EvidenceResolutionError, TypeError, ValueError) as exc:
            raise EvidenceMismatch(str(exc)) from exc

    def normalize(self, claim: VerifiedClaim) -> NormalizedClaim:
        if not isinstance(claim, VerifiedClaim):
            raise TypeError("claim must have verified literal evidence before normalization")
        self.calls.append("normalize")
        raw = claim.record
        semantic = _semantic_payload(raw)
        record = {
            "schema_version": "claim/v1",
            "id": _trim(raw["id"], label="id"),
            "fingerprint": sha256_bytes(canonical_json_bytes(semantic)),
            "text": _nfc(raw["text"]),
            **semantic,
            "observed_at": claim.claim.block.observed_at,
            "lifecycle": raw["lifecycle"],
            "confidence": raw["confidence"],
            "authority": raw["authority"],
            "evidence": json.loads(canonical_json_bytes(raw["evidence"])),
            "links": sorted({_trim(item, label="claim link") for item in raw["links"]}),
            "extractor_version": _trim(raw["extractor_version"], label="extractor version"),
        }
        validate_claim_record(record)
        return NormalizedClaim(record)


def _claim_source_daily_id(source: bytes) -> str:
    text = _decoded_claim_source(source)
    match = _DATE_RE.match(text)
    if match is None:
        raise ValueError("claim source has no canonical daily date")
    daily_id = match[1]
    try:
        date.fromisoformat(daily_id)
    except ValueError as exc:
        raise ValueError("claim source daily date is invalid") from exc
    return daily_id


def _decoded_claim_source(source: bytes) -> str:
    try:
        return source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("claim source is not UTF-8") from exc


def _extraction_records(result: Mapping[str, object]) -> list[object]:
    if (
        set(result) != {"schema_version", "claims"}
        or result.get("schema_version") != "claim-extraction/v1"
    ):
        raise ValueError("claim extraction schema envelope is invalid")
    records = result.get("claims")
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("claim extraction schema claims are invalid")
    return records


def _validated_record(raw: object) -> object:
    record = dict(raw) if isinstance(raw, Mapping) else raw
    _validate_extracted_record(record)
    return record


def _require_matching_reference(
    embedded: EvidenceRef, claim: Claim, reference: EvidenceRef | None
) -> None:
    if reference is not None and reference != embedded:
        raise EvidenceMismatch("explicit evidence reference does not match the claim")
    if (
        embedded.source_sha256 != claim.block.source_sha256
        or embedded.block_id != claim.block.block_id
    ):
        raise EvidenceMismatch("evidence reference does not match the immutable block")


def _require_verified_literal(
    claim: Claim, evidence: Mapping[str, object], resolved: object
) -> None:
    """The resolved span must be the exact bytes, and the exact text, of the claim."""
    if resolved.sha256 != evidence["sha256"]:
        raise EvidenceMismatch("evidence span hash does not match")
    literal = _evidence_literal(resolved.bytes)
    if literal != evidence["text"] or literal != claim.record["text"]:
        raise EvidenceMismatch("evidence literal text does not match")


def _evidence_literal(span: bytes) -> str:
    try:
        return span.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvidenceMismatch("evidence span is not UTF-8") from exc


def validate_claim_record(record: object) -> None:
    validate_schema({"schema_version": "claim-ledger/v1", "claims": [record]}, LEDGER_SCHEMA)
    assert isinstance(record, Mapping)
    _require_canonical_semantics(record)
    evidence = record["evidence"]
    assert isinstance(evidence, Mapping)
    ref = EvidenceRef.parse(evidence["reference"])
    if ref.byte_end <= ref.byte_start:
        raise ValueError("claim evidence span must be non-empty")
    _require_evidence_literal(record, evidence)
    _require_observation(record, ref)


def _require_canonical_semantics(record: Mapping[str, object]) -> None:
    semantic = _semantic_payload(record)
    if any(
        canonical_json_bytes(record[field]) != canonical_json_bytes(semantic[field])
        for field in semantic
    ):
        raise ValueError("claim semantic fields are not canonical")
    if record["fingerprint"] != sha256_bytes(canonical_json_bytes(semantic)):
        raise ValueError("claim fingerprint does not match normalized semantics")


def _require_evidence_literal(
    record: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    if evidence["text"] != record["text"] or sha256_bytes(
        evidence["text"].encode("utf-8")
    ) != evidence["sha256"]:
        raise ValueError("claim evidence literal hash does not match")


def _require_observation(record: Mapping[str, object], ref: EvidenceRef) -> None:
    observed_at = _strict_rfc3339_utc(record["observed_at"], label="observed_at")
    try:
        datetime.strptime(ref.block_id, "%H:%M:%S")
    except ValueError as exc:
        raise ValueError(
            "observed evidence block is not a valid HH:MM:SS timestamp"
        ) from exc
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", ref.block_id) is None or observed_at != (
        f"{ref.daily_id}T{ref.block_id}Z"
    ):
        raise ValueError("claim observation does not match its evidence block")


def parse_claim_ledger(content: bytes) -> dict[str, object] | None:
    text = _decoded_claim_page(content)
    if not _has_claims_heading(text):
        return None
    ledger = _parsed_ledger(text)
    validate_schema(ledger, LEDGER_SCHEMA)
    _require_unique_ledger_ids(ledger["claims"])
    for record in ledger["claims"]:
        validate_claim_record(record)
    return ledger


def _decoded_claim_page(content: bytes) -> str:
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("claim page is not UTF-8") from exc


def _has_claims_heading(text: str) -> bool:
    headings = list(re.finditer(r"(?m)^## Claims[ \t]*\r?$", text))
    if not headings:
        return False
    if len(headings) != 1:
        raise ValueError("claim page must contain exactly one Claims ledger")
    return True


def _parsed_ledger(text: str) -> dict[str, object]:
    match = _CLAIMS_RE.search(text)
    if match is None:
        raise ValueError("Claims ledger must be one fenced canonical JSON object")
    raw = match[1].encode("utf-8")
    try:
        ledger = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Claims ledger is malformed JSON") from exc
    if canonical_json_bytes(ledger) != raw:
        raise ValueError("Claims ledger is not restricted canonical JSON")
    return ledger


def _require_unique_ledger_ids(claims: list[object]) -> None:
    claim_ids = [str(record["id"]) for record in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim ledger contains a duplicate claim id")


def is_substantive(record: Mapping[str, object]) -> bool:
    evidence = record.get("evidence")
    if not _structurally_substantive(record, evidence):
        return False
    assert isinstance(evidence, Mapping)
    try:
        EvidenceRef.parse(evidence["reference"])
        return _literal_agrees(record, evidence)
    except (TypeError, ValueError):
        return False


def _structurally_substantive(record: Mapping[str, object], evidence: object) -> bool:
    relation = str(record.get("relation", "")).strip().casefold()
    if record.get("lifecycle") != "active":
        return False
    if relation not in RELATIONS or relation in _NON_SUBSTANTIVE_RELATIONS:
        return False
    return isinstance(evidence, Mapping) and set(evidence) == {
        "reference",
        "sha256",
        "text",
    }


def _literal_agrees(
    record: Mapping[str, object], evidence: Mapping[str, object]
) -> bool:
    literal = evidence["text"]
    if not isinstance(literal, str) or literal != record.get("text"):
        return False
    return sha256_bytes(literal.encode("utf-8")) == evidence["sha256"]


def page_may_auto_supersede(ledger: Mapping[str, object] | None) -> bool:
    if ledger is None or ledger.get("schema_version") != "claim-ledger/v1":
        return False
    claims = ledger.get("claims")
    if not isinstance(claims, list):
        return False
    return any(_substantive_entry(item) for item in claims)


def _substantive_entry(item: object) -> bool:
    return isinstance(item, Mapping) and is_substantive(item)


_EXPECTED_CLAIM_INDEX_SHAPES = (
    (
        ("id", "TEXT", 1, None, 2),
        ("fingerprint", "TEXT", 1, None, 0),
        ("subject", "TEXT", 1, None, 0),
        ("relation", "TEXT", 1, None, 0),
        ("lifecycle", "TEXT", 1, None, 0),
        ("page", "TEXT", 1, None, 1),
        ("record_json", "BLOB", 1, None, 0),
    ),
    (("schema_version", "TEXT", 1, None, 1),),
    (
        ("page", "TEXT", 1, None, 1),
        ("claim_id", "TEXT", 1, None, 2),
        ("code", "TEXT", 1, None, 0),
    ),
    ("subject", "relation", "lifecycle", "fingerprint"),
    (CLAIM_INDEX_SCHEMA_VERSION,),
)


def _claim_index_shapes(database: sqlite3.Connection) -> tuple[object, ...]:
    return (
        _table_shape(database, "claim"),
        _table_shape(database, "claim_index_meta"),
        _table_shape(database, "claim_index_diagnostic"),
        tuple(
            row[2]
            for row in database.execute("PRAGMA index_info(claim_candidate_lookup)")
        ),
        tuple(
            row[0]
            for row in database.execute("SELECT schema_version FROM claim_index_meta")
        ),
    )


def _table_shape(database: sqlite3.Connection, table: str) -> tuple[tuple, ...]:
    return tuple(
        tuple(row[index] for index in range(1, 6))
        for row in database.execute(f"PRAGMA table_info({table})")
    )


def _resolved_state_root(state_root: Path | None) -> Path:
    if state_root is not None:
        return Path(state_root)
    configured = os.environ.get("LLM_WIKI_STATE_ROOT") or os.environ.get("LLM_WIKI_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent


def _resolved_vault(vault: Path | None, state_root: Path) -> Path:
    """A vault beside the state root wins over the configured one."""
    if vault is not None:
        return Path(vault)
    sibling = state_root.parent
    if (sibling / "knowledge").is_dir():
        return sibling
    return Path(os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent))


def _require_allowed_root(root: Path, allowed: tuple[Path, ...]) -> Path:
    resolved = Path(root).resolve(strict=True)
    if resolved not in allowed:
        raise ValueError(
            "claim rebuild sequences must contain canonical knowledge roots"
        )
    if Path(root).is_symlink() or not Path(root).is_dir():
        raise PermissionError("claim rebuild root must be a regular directory")
    return resolved


def _project_pages(resolved: Path) -> list[Path]:
    return [
        page
        for page in resolved.rglob("*.md")
        if page.name in {"context.md", "journal.md", "state.md"}
    ]


def _require_rebuild_active(deadline: float, cancelled: Callable[[], bool] | None) -> None:
    if time.monotonic() >= deadline or bool(cancelled and cancelled()):
        raise TimeoutError("claim rebuild cancelled or deadline reached")


def _unresolved_code(exc: BaseException) -> str:
    if "ambiguous" in str(exc).casefold():
        return "evidence_ambiguous"
    return "evidence_unresolved"


def _literal_diagnostic(
    resolved: object, record: Mapping[str, object], evidence: Mapping[str, object]
) -> str | None:
    try:
        literal = resolved.bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "evidence_literal_mismatch"
    if _literal_disagrees(resolved, literal, record, evidence):
        return "evidence_literal_mismatch"
    return None


def _literal_disagrees(
    resolved: object,
    literal: str,
    record: Mapping[str, object],
    evidence: Mapping[str, object],
) -> bool:
    if resolved.sha256 != evidence["sha256"]:
        return True
    return literal != evidence["text"] or literal != record["text"]


def _require_candidate_limit(limit: object) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"candidate limit must be between 0 and {MAX_CANDIDATES}")
    if not 0 <= limit <= MAX_CANDIDATES:
        raise ValueError(f"candidate limit must be between 0 and {MAX_CANDIDATES}")


def _require_normalized_candidate(claim: object) -> None:
    if not isinstance(claim, NormalizedClaim):
        raise TypeError("candidate claim must be normalized")


def _require_active_bound(rows: Sequence[object]) -> None:
    """A derived reading must see whole groups, so it refuses instead of truncating."""
    if len(rows) > MAX_ACTIVE_RECORDS:
        raise ValueError(
            f"claim index holds more than {MAX_ACTIVE_RECORDS} active claims"
        )


class ClaimIndex:
    """A local owner-only SQLite projection; Markdown ledgers remain canonical."""

    def __init__(self, state_root: Path | None = None, *, vault: Path | None = None):
        self.state_root = _resolved_state_root(state_root).resolve(strict=False)
        self.vault = _resolved_vault(vault, self.state_root).resolve(strict=True)
        self.path = self.state_root / "cache" / "claims.sqlite3"
        self.lock_path = self.state_root / "cache" / "claims.rebuild.lock"
        self.resolver = EvidenceResolver(self.vault, state_root=self.state_root)

    def _connect(self) -> sqlite3.Connection:
        connection = open_operational_db(self.path, busy_ms=5000)
        try:
            _restrict_owner_only(self.path.parent, 0o700)
            _restrict_owner_only(self.path, 0o600)
            _verify_owner_only(self.path, 0o600)
            return connection
        except Exception:
            connection.close()
            raise

    def _page_bytes(self, page: Path) -> tuple[str, bytes]:
        resolved = Path(page).resolve(strict=True)
        allowed = (self.vault / "knowledge/notes", self.vault / "knowledge/projects")
        if not any(root == resolved or root in resolved.parents for root in allowed):
            raise PermissionError("claim index page is outside an allowed knowledge root")
        relative = resolved.relative_to(self.vault).as_posix()
        return relative, read_stable_bytes(
            Path(page), MAX_CLAIM_PAGE_BYTES, label="claim index page"
        )

    @staticmethod
    def _schema_compatible(database: sqlite3.Connection) -> bool:
        try:
            shapes = _claim_index_shapes(database)
        except sqlite3.Error:
            return False
        return shapes == _EXPECTED_CLAIM_INDEX_SHAPES

    @staticmethod
    def _replace_schema(database: sqlite3.Connection) -> None:
        statements = (
            "DROP INDEX IF EXISTS claim_candidate_lookup",
            "DROP TABLE IF EXISTS claim",
            "DROP TABLE IF EXISTS claim_index_meta",
            "DROP TABLE IF EXISTS claim_index_diagnostic",
            """CREATE TABLE claim (
                id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                subject TEXT NOT NULL,
                relation TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                page TEXT NOT NULL,
                record_json BLOB NOT NULL,
                PRIMARY KEY (page, id)
            )""",
            """CREATE INDEX claim_candidate_lookup
                ON claim(subject, relation, lifecycle, fingerprint)""",
            """CREATE TABLE claim_index_meta (
                schema_version TEXT NOT NULL PRIMARY KEY
            )""",
            """CREATE TABLE claim_index_diagnostic (
                page TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                code TEXT NOT NULL,
                PRIMARY KEY (page, claim_id)
            )""",
        )
        for statement in statements:
            database.execute(statement)
        database.execute(
            "INSERT INTO claim_index_meta(schema_version) VALUES(?)",
            (CLAIM_INDEX_SCHEMA_VERSION,),
        )

    def rebuild(
        self,
        sources: Sequence[Path] | Callable[[], Sequence[Path]] | None = None,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if time.monotonic() >= deadline or bool(cancelled and cancelled()):
            raise TimeoutError("claim rebuild cancelled or deadline reached")
        validate_state_root(self.path.parent)
        _restrict_owner_only(self.path.parent, 0o700)
        with _exclusive_file_lock(self.lock_path):
            pages = self._rebuild_pages(sources)
            self._rebuild_locked(pages, deadline=deadline, cancelled=cancelled)

    def _rebuild_pages(
        self,
        sources: Sequence[Path] | Callable[[], Sequence[Path]] | None,
    ) -> list[Path]:
        pages = self._discovered_pages(sources)
        if len(pages) > 10_000:
            raise ValueError("claim rebuild page discovery exceeds 10000 pages")
        if any(not isinstance(page, Path) for page in pages):
            raise TypeError("claim rebuild provider must return Path values")
        return sorted(pages)

    def _discovered_pages(
        self,
        sources: Sequence[Path] | Callable[[], Sequence[Path]] | None,
    ) -> list[Path]:
        if callable(sources):
            return list(sources())
        pages: list[Path] = []
        for root in self._rebuild_roots(sources):
            pages.extend(self._pages_under(root))
        return pages

    def _rebuild_roots(self, sources: Sequence[Path] | None) -> list[Path]:
        if sources is None:
            return list(self._allowed_roots())
        return list(sources)

    def _allowed_roots(self) -> tuple[Path, Path]:
        return (self.vault / "knowledge/notes", self.vault / "knowledge/projects")

    def _pages_under(self, root: Path) -> list[Path]:
        allowed = self._allowed_roots()
        resolved = _require_allowed_root(root, allowed)
        if resolved == allowed[0]:
            return list(resolved.rglob("*.md"))
        return _project_pages(resolved)

    def _rebuild_locked(
        self,
        pages: Sequence[Path],
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        rows: list[tuple[object, ...]] = []
        diagnostics: list[tuple[str, str, str]] = []
        seen_pages: set[str] = set()
        for page in pages:
            _require_rebuild_active(deadline, cancelled)
            self._collect_page(Path(page), seen_pages, rows, diagnostics)
        _require_rebuild_active(deadline, cancelled)
        self._write_index(rows, diagnostics)

    def _collect_page(
        self,
        page: Path,
        seen_pages: set[str],
        rows: list[tuple[object, ...]],
        diagnostics: list[tuple[str, str, str]],
    ) -> None:
        relative, content = self._page_bytes(page)
        if relative in seen_pages:
            raise ValueError("claim index page list contains duplicates")
        seen_pages.add(relative)
        ledger = parse_claim_ledger(content)
        if ledger is None:
            return
        for record in ledger["claims"]:
            self._collect_record(relative, record, rows, diagnostics)

    def _collect_record(
        self,
        relative: str,
        record: Mapping[str, object],
        rows: list[tuple[object, ...]],
        diagnostics: list[tuple[str, str, str]],
    ) -> None:
        code = self._evidence_diagnostic(record)
        if code is not None:
            diagnostics.append((relative, str(record["id"]), code))
            return
        rows.append(
            (
                record["id"],
                record["fingerprint"],
                record["subject"],
                record["relation"],
                record["lifecycle"],
                relative,
                canonical_json_bytes(record),
            )
        )

    def _evidence_diagnostic(self, record: Mapping[str, object]) -> str | None:
        """None means the record is indexable; a code says why it is not."""
        if record["lifecycle"] != "active":
            return None
        evidence = record["evidence"]
        try:
            resolved = self.resolver.resolve(evidence["reference"])
        except (EvidenceResolutionError, OSError, TypeError, ValueError) as exc:
            return _unresolved_code(exc)
        return _literal_diagnostic(resolved, record, evidence)

    def _write_index(
        self,
        rows: list[tuple[object, ...]],
        diagnostics: list[tuple[str, str, str]],
    ) -> None:
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                self._replace_rows(database, rows, diagnostics)
            except BaseException:
                database.rollback()
                raise
            else:
                database.commit()

    def _replace_rows(
        self,
        database: sqlite3.Connection,
        rows: list[tuple[object, ...]],
        diagnostics: list[tuple[str, str, str]],
    ) -> None:
        if self._schema_compatible(database):
            database.execute("DELETE FROM claim")
            database.execute("DELETE FROM claim_index_diagnostic")
        else:
            self._replace_schema(database)
        database.executemany(
            "INSERT INTO claim(id,fingerprint,subject,relation,lifecycle,page,record_json) VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        database.executemany(
            "INSERT INTO claim_index_diagnostic(page,claim_id,code) VALUES(?,?,?)",
            sorted(diagnostics),
        )

    def diagnostics(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with closing(self._connect()) as database:
            if not self._schema_compatible(database):
                return []
            rows = database.execute(
                "SELECT page, claim_id, code FROM claim_index_diagnostic "
                "ORDER BY page, claim_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def candidates(
        self, claim: NormalizedClaim | None, *, limit: int = MAX_CANDIDATES
    ) -> list[IndexedClaim]:
        _require_candidate_limit(limit)
        if limit == 0:
            return []
        _require_normalized_candidate(claim)
        if not self.path.exists():
            return []
        return [
            IndexedClaim(row["page"], NormalizedClaim(json.loads(row["record_json"])))
            for row in self._candidate_rows(claim, limit)
        ]

    def active_records(self, *, subject: str | None = None) -> list[IndexedClaim]:
        """Every active claim, for readings that must see a whole fact's history.

        `candidates` answers "what might this new claim collide with" and is
        bounded for that. A bitemporal reading cannot be truncated the same way:
        a missing successor would silently turn a superseded claim into a
        current one, so this refuses past its bound rather than return a prefix.
        """
        if not self.path.exists():
            return []
        return [
            IndexedClaim(row["page"], NormalizedClaim(json.loads(row["record_json"])))
            for row in self._active_rows(subject)
        ]

    def _active_rows(self, subject: str | None) -> list[object]:
        with closing(self._connect()) as database:
            if not self._schema_compatible(database):
                return []
            rows = database.execute(
                """
                SELECT page, record_json FROM claim
                WHERE lifecycle='active' AND (:subject IS NULL OR subject=:subject)
                ORDER BY page, id
                LIMIT :limit
                """,
                {"subject": subject, "limit": MAX_ACTIVE_RECORDS + 1},
            ).fetchall()
        _require_active_bound(rows)
        return rows

    def _candidate_rows(self, claim: NormalizedClaim, limit: int) -> list[object]:
        with closing(self._connect()) as database:
            if not self._schema_compatible(database):
                return []
            return database.execute(
                """
                SELECT page, record_json FROM claim
                WHERE lifecycle='active' AND (subject=? OR relation=? OR fingerprint=?)
                ORDER BY CASE WHEN fingerprint=? THEN 0 WHEN subject=? THEN 1 ELSE 2 END,
                         page, id
                LIMIT ?
                """,
                (
                    claim.record["subject"],
                    claim.record["relation"],
                    claim.record["fingerprint"],
                    claim.record["fingerprint"],
                    claim.record["subject"],
                    limit,
                ),
            ).fetchall()


@contextmanager
def _exclusive_file_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Hold an OS-released cross-process lock for a complete index rebuild."""
    path = Path(path)
    descriptor, created = _open_lock_file(path)
    acquired = False
    try:
        _prepare_lock_file(path, descriptor, created)
        _acquire_lock(descriptor, path, timeout)
        acquired = True
        _write_lock_token(descriptor)
        yield
    finally:
        _release_lock(descriptor, acquired)


def _open_lock_file(path: Path) -> tuple[int, bool]:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        return os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600), True
    except FileExistsError:
        return os.open(path, flags), False


def _prepare_lock_file(path: Path, descriptor: int, created: bool) -> None:
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"0")
    if created:
        _restrict_owner_only(path, 0o600)
        return
    _verify_owner_only(path, 0o600)


def _acquire_lock(descriptor: int, path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            _lock_region(descriptor)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"could not acquire claim rebuild lock: {path}"
                ) from exc
            time.sleep(0.05)


def _lock_region(descriptor: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_region(descriptor: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _write_lock_token(descriptor: int) -> None:
    token = f"{os.getpid()}:{threading.get_ident()}".encode()
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, token)
    os.ftruncate(descriptor, len(token))


def _release_lock(descriptor: int, acquired: bool) -> None:
    try:
        if acquired:
            os.lseek(descriptor, 0, os.SEEK_SET)
            _unlock_region(descriptor)
    finally:
        os.close(descriptor)
