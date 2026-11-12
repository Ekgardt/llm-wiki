"""Parse and validate the claim ledger embedded in a Markdown page."""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from claim_tree_manifest import MAX_CLAIM_TREE_FILE_BYTES
from iso_time import names_block
from llm_wiki.evidence_reference import EvidenceRef
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema, validate_schema_object
from llm_wiki.markdown_fences import closing_fence, opening_fence

SCHEMA_DIR = Path(__file__).parents[1] / "schemas"


LEDGER_SCHEMA = SCHEMA_DIR / "claim-ledger-v1.json"


LEDGER_V2_SCHEMA = SCHEMA_DIR / "claim-ledger-v2.json"


_LEDGER_SCHEMAS = {"claim-ledger/v1": LEDGER_SCHEMA, "claim-ledger/v2": LEDGER_V2_SCHEMA}


_RECORD_SCHEMA_RULES = {
    "claim/v1": json.loads(LEDGER_SCHEMA.read_text(encoding="utf-8"))["properties"]["claims"]["items"],
    "claim/v2": json.loads(LEDGER_V2_SCHEMA.read_text(encoding="utf-8"))["properties"]["claims"]["items"]["oneOf"][1],
}


# The claim tree's ceiling, which is the journal's: a project journal is one of
# the pages this index reads, and a page the journal accepts must never be one
# the index refuses. Measured 2026-09-10: a 4.2 MB journal refused every compile
# for three days after the claim tree's own cap had already been raised. See
# `docs/research/2026-09-10-one-ceiling-for-every-reader-of-a-journal.md`.
MAX_CLAIM_PAGE_BYTES = MAX_CLAIM_TREE_FILE_BYTES


# A claim's number value is model-written text: characters, digits and exponent are bounded so
# normalising `1e999999` can never expand into a huge integer (a resource bound on untrusted
# input). Basis unknown: value predates measurement; review when a real measured quantity is
# refused.
MAX_DECIMAL_CHARS = 128
MAX_DECIMAL_DIGITS = 128
MAX_DECIMAL_EXPONENT = 128


# The one definition of a Claims ledger, for its readers and its writers alike:
# opening, the canonical JSON line, closing fence — then only blank lines before
# the next section or the end. Three copies of this once required exactly one
# line break, and compile's own `## Update` after a blank line made every such
# page unreadable. See `docs/research/2026-09-14-a-blank-line-before-the-next-section.md`.
CLAIM_LEDGER_RE = re.compile(
    rb"(?ms)(^## Claims[ \t]*\r?\n```json[ \t]*\r?\n)([^\r\n]+)"
    rb"(\r?\n```[ \t]*(?=(?:\r?\n[ \t]*)*(?:\r?\n## |\Z)))"
)


_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


_DECIMAL_INPUT_RE = re.compile(
    r"^[+-]?(?:(?P<integer>\d+)(?:\.(?P<fraction>\d*))?|"
    r"\.(?P<leading_fraction>\d+))(?:[eE](?P<exponent>[+-]?\d+))?$"
)


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def trim(value: object, *, label: str, fold: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    result = _folded(nfc(" ".join(value.split())), fold)
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _folded(text: str, fold: bool) -> str:
    if fold:
        return text.casefold()
    return text


def canonical_time(value: object, *, nullable: bool, label: str) -> str | None:
    if value is None and nullable:
        return None
    text = trim(value, label=label)
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


def _canonical_timestamp_text(text: str, *, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC date or timestamp") from exc
    return parsed.isoformat().replace("+00:00", "Z").replace(".000000Z", "Z")


def strict_rfc3339_utc(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a strict UTC RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a real RFC3339 date/time") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{label} must be UTC")
    return value


def canonical_decimal(value: object) -> str:
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


def normalize_value(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get("type"), str):
        raise ValueError("claim value must be typed")
    kind = value["type"]
    _require_value_fields(value, kind)
    raw = value["value"]
    if kind == "number":
        return {
            "type": "number",
            "value": canonical_decimal(raw),
            "unit": trim(value["unit"], label="number unit", fold=True),
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
    return trim(raw, label="string value")


def _normalized_entity(raw: object) -> object:
    return trim(raw, label="entity value", fold=True)


def _normalized_boolean(raw: object) -> bool:
    if not isinstance(raw, bool):
        raise ValueError("boolean value is invalid")
    return raw


def _normalized_date(raw: object) -> object:
    result = canonical_time(raw, nullable=False, label="date value")
    if "T" in str(result):
        raise ValueError("date value must not contain a time")
    return result


def _normalized_timestamp(raw: object) -> object:
    result = canonical_time(raw, nullable=False, label="timestamp value")
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


def validate_interval(validity: object) -> dict[str, str | None]:
    if not isinstance(validity, Mapping) or set(validity) != {"from", "to"}:
        raise ValueError("claim validity must contain exactly from and to")
    start = canonical_time(validity["from"], nullable=True, label="validity from")
    end = canonical_time(validity["to"], nullable=True, label="validity to")
    require_ordered_interval(start, end)
    return {"from": start, "to": end}


def require_ordered_interval(start: str | None, end: str | None) -> None:
    if start is None or end is None:
        return
    if instant_of(start) >= instant_of(end):
        raise ValueError("claim validity must be a non-empty half-open interval")


def instant_of(value: str) -> datetime:
    """A bare date is its first instant; canonical times order by instant, not text.

    Text ordering is wrong here because canonical form keeps fractional seconds
    and `.` sorts before `Z`, so `…:00.5Z` compares as earlier than `…:00Z`.
    """
    if "T" in value:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    return datetime.fromisoformat(f"{value}T00:00:00+00:00")


def semantic_payload(record: Mapping[str, object]) -> dict[str, object]:
    qualifiers = [
        {
            "key": trim(item["key"], label="qualifier key", fold=True),
            "value": normalize_value(item["value"]),
        }
        for item in record["qualifiers"]
    ]
    qualifiers.sort(key=canonical_json_bytes)
    return {
        "subject": trim(record["subject"], label="subject", fold=True),
        "relation": trim(record["relation"], label="relation", fold=True),
        "value": normalize_value(record["value"]),
        "qualifiers": qualifiers,
        "validity": validate_interval(record["validity"]),
    }


def _requires_literal_encoding(value: object) -> bool:
    if isinstance(value, (list, tuple)):
        return any(_requires_literal_encoding(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    return value.get("schema_version") in ("claim/v2", "claim-ledger/v2") or _embedded_literal_encoding(value)


def _embedded_literal_encoding(value: Mapping[str, object]) -> bool:
    return any(_requires_literal_encoding(item) for item in value.get("claims", [])) or _requires_literal_encoding(value.get("claim")) or _has_native_evidence(value)


def _has_native_evidence(value: Mapping[str, object]) -> bool:
    return "native_event" in value or any(isinstance(item, Mapping) and "native_event" in item for item in value.get("evidence", []))


def claim_json_bytes(value: object) -> bytes:
    """Historical v1 canonical encoding, or exact v2 physical literal strings.

    Accepts a validated claim, ledger or existing candidate wrapper. Semantic
    fingerprints still use canonical_json_bytes; this encoder never grants proof.
    """
    if _requires_literal_encoding(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
    return canonical_json_bytes(value)


def require_claim_byte_budget(content: bytes) -> None:
    if len(content) > MAX_CLAIM_PAGE_BYTES:
        raise ValueError("claim content exceeds the existing page byte budget")


def ledger_schema_path(ledger: object) -> Path:
    if not isinstance(ledger, Mapping):
        raise ValueError("claim ledger must be an object")
    path = _LEDGER_SCHEMAS.get(_document_version(ledger))
    if path is None:
        raise ValueError("unsupported claim ledger version")
    return path


def claim_record_schema(record: object) -> dict:
    if not isinstance(record, Mapping):
        raise ValueError("claim record must be an object")
    version = _document_version(record)
    if version not in _RECORD_SCHEMA_RULES:
        raise ValueError("unsupported claim record version")
    return deepcopy(_RECORD_SCHEMA_RULES[version])


def _document_version(document: Mapping[str, object]) -> str:
    version = document.get("schema_version")
    if not isinstance(version, str):
        raise ValueError("claim document version must be a string")
    return version


def validate_claim_record(record: object) -> None:
    require_claim_byte_budget(claim_json_bytes(record))
    validate_schema_object(record, claim_record_schema(record))
    assert isinstance(record, Mapping)
    _require_canonical_semantics(record)
    evidence = record["evidence"]
    assert isinstance(evidence, Mapping)
    ref = EvidenceRef.parse(evidence["reference"])
    if ref.byte_end <= ref.byte_start:
        raise ValueError("claim evidence span must be non-empty")
    _require_evidence_literal(record, evidence)
    require_observation(record, ref)


def _require_canonical_semantics(record: Mapping[str, object]) -> None:
    semantic = semantic_payload(record)
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


def require_observation(record: Mapping[str, object], ref: EvidenceRef) -> None:
    observed_at = strict_rfc3339_utc(record["observed_at"], label="observed_at")
    try:
        datetime.strptime(ref.block_id, "%H:%M:%S")
    except ValueError as exc:
        raise ValueError(
            "observed evidence block is not a valid HH:MM:SS timestamp"
        ) from exc
    well_formed = re.fullmatch(r"\d{2}:\d{2}:\d{2}", ref.block_id) is not None
    if not (well_formed and names_block(observed_at, ref.daily_id, ref.block_id)):
        raise ValueError("claim observation does not match its evidence block")


def parse_claim_ledger(content: bytes) -> dict[str, object] | None:
    match = claim_ledger_match(content)
    if match is None:
        return None
    ledger = _parsed_ledger(match)
    validate_schema(ledger, ledger_schema_path(ledger))
    _require_unique_ledger_ids(ledger["claims"])
    for record in ledger["claims"]:
        validate_claim_record(record)
    return ledger


def decoded_claim_page(content: bytes) -> str:
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("claim page is not UTF-8") from exc


_REFLECTION_HEADER = re.compile(rb"## History \(pre-reflection (\d{4}-\d{2}-\d{2})\)[ \t]*")


_REFLECTION_SUMMARY = b"<summary>Original page before reflection</summary>"


_CLAIMS_HEADING = re.compile(rb"## Claims[ \t]*")


_DETAILS_OPEN = re.compile(rb"<details>(?:[ \t]*<summary>.*</summary>)?")


class _ClaimPageScan:
    """Only the producer-owned historical container is excluded from authority."""

    def __init__(self):
        self.fence = None
        self.depth = 0
        self.pending = None
        self.start = None
        self.summary_pending = False
        self.headings = []
        self.histories = []
        self.history_markers = []

    def line(self, body, start, end):
        if self.pending is not None or self.summary_pending:
            self._container_header(body)
            return
        if self._fenced(body):
            return
        self._body_line(body, start, end)

    def _container_header(self, body):
        if self.pending is not None:
            self._begin_history(body)
            return
        self._summary(body)

    def _body_line(self, body, start, end):
        if self.depth:
            self._history_line(body, end)
            return
        self._active_line(body, start)

    def _fenced(self, body):
        if self.fence is not None:
            character, length = self.fence
            if closing_fence(body, character, length):
                self.fence = None
            return True
        opening = opening_fence(body)
        return self._opened_fence(opening)

    def _opened_fence(self, opening):
        if opening is None:
            return False
        self.fence = (opening[1][:1], len(opening[1]))
        return True

    def _active_line(self, body, start):
        _require_no_orphan_history_close(body, self.histories)
        self._note_history_marker(body, start)
        if _reflection_header(body):
            self.pending = start
            return
        if _CLAIMS_HEADING.fullmatch(body):
            self.headings.append(start)

    def _note_history_marker(self, body, start):
        if body.startswith(b"## History (pre-reflection"):
            self.history_markers.append(start)

    def _begin_history(self, body):
        if not body.strip():
            return
        _require_reflection_open(body)
        self.start, self.pending, self.depth = self.pending, None, 1
        self.summary_pending = body == b"<details>"

    def _history_line(self, body, end):
        self.depth += _details_delta(body)
        if self.depth == 0:
            self.histories.append((self.start, end))
            self.start = None

    def _summary(self, body):
        if not body.strip():
            return
        if body != _REFLECTION_SUMMARY:
            raise ValueError("reflection history has no canonical summary")
        self.summary_pending = False

    def finish(self):
        if self.pending is not None or self.depth:
            raise ValueError("reflection history is malformed or unclosed")


def _reflection_header(body):
    matched = _REFLECTION_HEADER.fullmatch(body)
    if matched is None:
        return False
    date.fromisoformat(matched[1].decode("ascii"))
    return True


def _require_reflection_open(body):
    allowed = (b"<details>", b"<details>" + _REFLECTION_SUMMARY)
    if body not in allowed:
        raise ValueError("reflection history has no canonical details container")


def _require_no_orphan_history_close(body, histories):
    if histories and body == b"</details>":
        raise ValueError("reflection history has an unmatched closing container")


def _details_delta(body):
    if body == b"</details>":
        return -1
    return int(_DETAILS_OPEN.fullmatch(body) is not None)


def claim_page_scan(content):
    scan, offset = _ClaimPageScan(), 0
    for line in content.splitlines(keepends=True):
        end = offset + len(line)
        scan.line(line.rstrip(b"\r\n"), offset, end)
        offset = end
    scan.finish()
    return scan


def _active_claim_match(content, scan):
    if not scan.headings:
        return None
    if len(scan.headings) != 1:
        raise ValueError("claim page must contain exactly one Claims ledger")
    masked = bytearray(content)
    for start, end in scan.histories:
        masked[start:end] = re.sub(rb"[^\r\n]", b" ", content[start:end])
    matched = CLAIM_LEDGER_RE.match(bytes(masked), scan.headings[0])
    return _require_claim_match(matched)


def _require_claim_match(matched):
    if matched is None:
        raise ValueError("Claims ledger must be one fenced canonical JSON object")
    return matched


def claim_ledger_match(content: bytes):
    """The active canonical ledger with original byte offsets, never history."""
    require_claim_byte_budget(content)
    decoded_claim_page(content)
    return _active_claim_match(content, claim_page_scan(content))


def _parsed_ledger(match) -> dict[str, object]:
    raw = match[2]
    try:
        ledger = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Claims ledger is malformed JSON") from exc
    if claim_json_bytes(ledger) != raw:
        raise ValueError("Claims ledger is not restricted canonical JSON")
    return ledger


def _require_unique_ledger_ids(claims: list[object]) -> None:
    claim_ids = [str(record["id"]) for record in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim ledger contains a duplicate claim id")
