"""Evidence-verified atomic claims and their rebuildable derived index."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from bounded_io import read_stable_bytes
from compile_cache import _restrict_owner_only, _verify_owner_only
from evidence_resolver import EvidenceRef, EvidenceResolutionError, EvidenceResolver
from reliable_memory import (
    canonical_json_bytes,
    open_operational_db,
    sha256_bytes,
    validate_schema,
)

SCHEMA_DIR = Path(__file__).with_name("schemas")
LEDGER_SCHEMA = SCHEMA_DIR / "claim-ledger-v1.json"
CANDIDATE_SCHEMA = SCHEMA_DIR / "claim-candidate-v1.json"
RELATION_SCHEMA = SCHEMA_DIR / "claim-relations-v1.json"
MAX_CLAIM_PAGE_BYTES = 4 * 1024 * 1024
MAX_CANDIDATES = 50
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
_DATE_RE = re.compile(r"^# (\d{4}-\d{2}-\d{2})(?:\r?\n|$)")
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
    result = _nfc(" ".join(value.split()))
    if fold:
        result = result.casefold()
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _canonical_time(value: object, *, nullable: bool, label: str) -> str | None:
    if value is None and nullable:
        return None
    text = _trim(value, label=label)
    try:
        if "T" not in text:
            if date.fromisoformat(text).isoformat() != text:
                raise ValueError
            return text
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC date or timestamp") from exc
    canonical = parsed.isoformat().replace("+00:00", "Z")
    return canonical.replace(".000000Z", "Z")


def _normalize_value(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
        raise ValueError("claim value must be typed")
    kind = value["type"]
    expected = {"type", "value", "unit"} if kind == "number" else {"type", "value"}
    if set(value) != expected:
        raise ValueError("typed claim value fields are invalid")
    raw = value["value"]
    if kind == "string":
        result: object = _trim(raw, label="string value") if raw != "" else ""
    elif kind == "entity":
        result = _trim(raw, label="entity value", fold=True)
    elif kind == "boolean":
        if not isinstance(raw, bool):
            raise ValueError("boolean value is invalid")
        result = raw
    elif kind == "number":
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(raw):
            raise ValueError("number value is invalid")
        unit = _trim(value["unit"], label="number unit", fold=True)
        return {"type": "number", "value": raw, "unit": unit}
    elif kind == "date":
        result = _canonical_time(raw, nullable=False, label="date value")
        if "T" in str(result):
            raise ValueError("date value must not contain a time")
    elif kind == "timestamp":
        result = _canonical_time(raw, nullable=False, label="timestamp value")
        if "T" not in str(result):
            raise ValueError("timestamp value must contain a time")
    else:
        raise ValueError("claim value type is invalid")
    return {"type": kind, "value": result}


def _validate_interval(validity: object) -> dict[str, str | None]:
    if not isinstance(validity, Mapping) or set(validity) != {"from", "to"}:
        raise ValueError("claim validity must contain exactly from and to")
    start = _canonical_time(validity["from"], nullable=True, label="validity from")
    end = _canonical_time(validity["to"], nullable=True, label="validity to")
    if start is not None and end is not None:
        start_cmp = f"{start}T00:00:00Z" if "T" not in start else start
        end_cmp = f"{end}T00:00:00Z" if "T" not in end else end
        if start_cmp >= end_cmp:
            raise ValueError("claim validity must be a non-empty half-open interval")
    return {"from": start, "to": end}


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
    relation = _trim(record["relation"], label="relation", fold=True)
    if relation not in RELATIONS:
        raise ValueError("claim extraction schema relation is invalid")
    _normalize_value(record["value"])
    qualifiers = record["qualifiers"]
    if not isinstance(qualifiers, list) or len(qualifiers) > 100:
        raise ValueError("claim extraction schema qualifiers are invalid")
    for qualifier in qualifiers:
        if not isinstance(qualifier, dict) or set(qualifier) != {"key", "value"}:
            raise ValueError("claim extraction schema qualifier fields are invalid")
        _trim(qualifier["key"], label="qualifier key")
        _normalize_value(qualifier["value"])
    _validate_interval(record["validity"])
    if record["lifecycle"] not in {"active", "superseded", "inactive", "quarantined"}:
        raise ValueError("claim extraction schema lifecycle is invalid")
    if record["confidence"] not in {"high", "medium", "low"}:
        raise ValueError("claim extraction schema confidence is invalid")
    if record["authority"] not in {"user", "web", "ai-derived", "inferred"}:
        raise ValueError("claim extraction schema authority is invalid")
    evidence = record["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"reference", "sha256", "text"}:
        raise ValueError("claim extraction schema evidence fields are invalid")
    EvidenceRef.parse(evidence["reference"])
    if not isinstance(evidence["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"]) is None:
        raise ValueError("claim extraction schema evidence hash is invalid")
    _trim(evidence["text"], label="evidence text")
    links = record["links"]
    if not isinstance(links, list) or len(links) > 100 or any(not isinstance(item, str) or not item for item in links):
        raise ValueError("claim extraction schema links are invalid")


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
        try:
            text = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("claim source is not UTF-8") from exc
        date_match = _DATE_RE.match(text)
        if date_match is None:
            raise ValueError("claim source has no canonical daily date")
        daily_id = date_match[1]
        try:
            date.fromisoformat(daily_id)
        except ValueError as exc:
            raise ValueError("claim source daily date is invalid") from exc
        raw_headers = list(re.finditer(rb"(?m)^## \[([^\]\r\n]+)\]", source))
        matches = list(_BLOCK_RE.finditer(source))
        if len(matches) != len(raw_headers):
            raise ValueError("claim block timestamp is invalid")
        digest = sha256_bytes(source)
        blocks: list[TimestampBlock] = []
        for index, match in enumerate(matches):
            block_id = match[1].decode("ascii")
            try:
                datetime.strptime(block_id, "%H:%M:%S")
            except ValueError as exc:
                raise ValueError("claim block timestamp is invalid") from exc
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            blocks.append(
                TimestampBlock(
                    source,
                    digest,
                    daily_id,
                    block_id,
                    f"{daily_id}T{block_id}Z",
                    match.start(),
                    end,
                    source[match.start() : end],
                )
            )
        return tuple(blocks)

    def extract(
        self, block: TimestampBlock, result: Mapping[str, object]
    ) -> tuple[Claim, ...]:
        self.calls.append("extract")
        if not isinstance(block, TimestampBlock) or not isinstance(result, Mapping):
            raise TypeError("claim extraction inputs are invalid")
        if set(result) != {"schema_version", "claims"} or result.get("schema_version") != "claim-extraction/v1":
            raise ValueError("claim extraction schema envelope is invalid")
        records = result.get("claims")
        if not isinstance(records, list) or len(records) > 1000:
            raise ValueError("claim extraction schema claims are invalid")
        claims: list[Claim] = []
        for raw in records:
            record = dict(raw) if isinstance(raw, Mapping) else raw
            _validate_extracted_record(record)
            claims.append(Claim(record, block))
        return tuple(claims)

    def verify_literal(
        self, claim: Claim, reference: EvidenceRef | None = None
    ) -> VerifiedClaim:
        self.calls.append("verify_evidence")
        if not isinstance(claim, Claim):
            raise TypeError("claim must be extracted before evidence verification")
        evidence = claim.record["evidence"]
        assert isinstance(evidence, dict)
        try:
            embedded = EvidenceRef.parse(evidence["reference"])
            if reference is not None and reference != embedded:
                raise EvidenceMismatch("explicit evidence reference does not match the claim")
            if embedded.source_sha256 != claim.block.source_sha256 or embedded.block_id != claim.block.block_id:
                raise EvidenceMismatch("evidence reference does not match the immutable block")
            resolved = self.resolver.resolve(embedded)
        except EvidenceMismatch:
            raise
        except (EvidenceResolutionError, TypeError, ValueError) as exc:
            raise EvidenceMismatch(str(exc)) from exc
        if resolved.sha256 != evidence["sha256"]:
            raise EvidenceMismatch("evidence span hash does not match")
        try:
            literal = resolved.bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise EvidenceMismatch("evidence span is not UTF-8") from exc
        if literal != evidence["text"] or literal != claim.record["text"]:
            raise EvidenceMismatch("evidence literal text does not match")
        return VerifiedClaim(claim, resolved.bytes)

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


def validate_claim_record(record: object) -> None:
    validate_schema({"schema_version": "claim-ledger/v1", "claims": [record]}, LEDGER_SCHEMA)
    assert isinstance(record, Mapping)
    semantic = _semantic_payload(record)
    if any(
        canonical_json_bytes(record[field]) != canonical_json_bytes(semantic[field])
        for field in semantic
    ):
        raise ValueError("claim semantic fields are not canonical")
    if record["fingerprint"] != sha256_bytes(canonical_json_bytes(semantic)):
        raise ValueError("claim fingerprint does not match normalized semantics")
    evidence = record["evidence"]
    assert isinstance(evidence, Mapping)
    ref = EvidenceRef.parse(evidence["reference"])
    if ref.byte_end <= ref.byte_start:
        raise ValueError("claim evidence span must be non-empty")
    if evidence["text"] != record["text"] or sha256_bytes(
        evidence["text"].encode("utf-8")
    ) != evidence["sha256"]:
        raise ValueError("claim evidence literal hash does not match")
    if record["observed_at"] != f"{ref.daily_id}T{ref.block_id}Z":
        raise ValueError("claim observation does not match its evidence block")


def parse_claim_ledger(content: bytes) -> dict[str, object] | None:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("claim page is not UTF-8") from exc
    headings = list(re.finditer(r"(?m)^## Claims[ \t]*\r?$", text))
    if not headings:
        return None
    if len(headings) != 1:
        raise ValueError("claim page must contain exactly one Claims ledger")
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
    validate_schema(ledger, LEDGER_SCHEMA)
    for record in ledger["claims"]:
        validate_claim_record(record)
    return ledger


def is_substantive(record: Mapping[str, object]) -> bool:
    relation = str(record.get("relation", "")).strip().casefold()
    evidence = record.get("evidence")
    structurally_substantive = (
        record.get("lifecycle") == "active"
        and relation in RELATIONS
        and relation not in _NON_SUBSTANTIVE_RELATIONS
        and isinstance(evidence, Mapping)
        and set(evidence) == {"reference", "sha256", "text"}
    )
    if not structurally_substantive:
        return False
    try:
        EvidenceRef.parse(evidence["reference"])
        literal = evidence["text"]
        return (
            isinstance(literal, str)
            and literal == record.get("text")
            and sha256_bytes(literal.encode("utf-8")) == evidence["sha256"]
        )
    except (TypeError, ValueError):
        return False


def page_may_auto_supersede(ledger: Mapping[str, object] | None) -> bool:
    if ledger is None or ledger.get("schema_version") != "claim-ledger/v1":
        return False
    claims = ledger.get("claims")
    return isinstance(claims, list) and any(
        isinstance(item, Mapping) and is_substantive(item) for item in claims
    )


class ClaimIndex:
    """A local owner-only SQLite projection; Markdown ledgers remain canonical."""

    def __init__(self, state_root: Path | None = None, *, vault: Path | None = None):
        if state_root is None:
            configured = os.environ.get("LLM_WIKI_STATE_ROOT") or os.environ.get("LLM_WIKI_ROOT")
            state_root = Path(configured) if configured else Path(__file__).resolve().parent.parent
        self.state_root = Path(state_root).resolve(strict=False)
        if vault is None:
            sibling = self.state_root.parent
            configured_vault = Path(os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent))
            vault = sibling if (sibling / "knowledge").is_dir() else configured_vault
        self.vault = Path(vault).resolve(strict=True)
        self.path = self.state_root / "cache" / "claims.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        connection = open_operational_db(self.path, busy_ms=5000)
        try:
            _restrict_owner_only(self.path.parent, 0o700)
            _restrict_owner_only(self.path, 0o600)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS claim (
                    id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    page TEXT NOT NULL,
                    record_json BLOB NOT NULL,
                    PRIMARY KEY (page, id)
                );
                CREATE INDEX IF NOT EXISTS claim_candidate_lookup
                    ON claim(subject, relation, lifecycle, fingerprint);
                """
            )
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

    def rebuild(self, pages: Sequence[Path]) -> None:
        rows: list[tuple[object, ...]] = []
        seen_pages: set[str] = set()
        for page in pages:
            relative, content = self._page_bytes(Path(page))
            if relative in seen_pages:
                raise ValueError("claim index page list contains duplicates")
            seen_pages.add(relative)
            ledger = parse_claim_ledger(content)
            if ledger is None:
                continue
            for record in ledger["claims"]:
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
        with closing(self._connect()) as database, database:
            database.execute("DELETE FROM claim")
            database.executemany(
                "INSERT INTO claim(id,fingerprint,subject,relation,lifecycle,page,record_json) VALUES(?,?,?,?,?,?,?)",
                rows,
            )

    def candidates(
        self, claim: NormalizedClaim | None, *, limit: int = MAX_CANDIDATES
    ) -> list[IndexedClaim]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_CANDIDATES:
            raise ValueError(f"candidate limit must be between 0 and {MAX_CANDIDATES}")
        if limit == 0:
            return []
        if not isinstance(claim, NormalizedClaim):
            raise TypeError("candidate claim must be normalized")
        if not self.path.exists():
            return []
        with closing(self._connect()) as database:
            rows = database.execute(
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
        return [
            IndexedClaim(row["page"], NormalizedClaim(json.loads(row["record_json"])))
            for row in rows
        ]
