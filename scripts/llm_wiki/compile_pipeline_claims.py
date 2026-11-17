"""The compile's outcomes, derived claims, quote verification and page rendering."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date as calendar_date
from datetime import datetime
from typing import TYPE_CHECKING

from claims import (
    claim_json_bytes,
    claim_ledger_document,
    validate_claim_record,
)
from compile_receipts import (
    DailySnapshot,
    SourceSnapshot,
    physical_source,
)
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
    EvidenceRef,
    EvidenceResolver,
    daily_entries,
)
from llm_wiki.claim_ledger import (
    claim_ledger_match,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes  # noqa: E402

if TYPE_CHECKING:
    pass
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_base import (
    CATEGORY_SINGULAR,
    CLAIM_RECORD_SCHEMA,
    NATIVE_EVIDENCE_SCHEMA,
    SOURCE_CHOICE_RESOLVER,
)
from llm_wiki.compile_pipeline_sources import (
    MAX_FAILURE_DETAIL_CHARS,
    CompileApplyResult,
    CompileInputs,
    require_native_unit,
)


def reference_source(inputs: CompileInputs, reference: EvidenceRef) -> SourceSnapshot:
    matched = reference_parts(inputs, reference)
    return physical_source(matched[0])


def reference_parts(inputs, reference):
    parts = dailies_for_evidence(inputs, reference.daily_id)
    matched = sorted((part for part in parts if _part_intersects_reference(part, reference)),
                     key=lambda part: part.byte_start)
    if not matched:
        raise ValueError("compile claim evidence source is absent from the snapshot")
    _require_reference_cover(matched, reference)
    return matched


def _part_intersects_reference(part, reference):
    if physical_source(part).sha256 != reference.source_sha256:
        return False
    if part.original_content is None:
        return 0 <= reference.byte_start < reference.byte_end <= len(part.content)
    return part.byte_start < reference.byte_end and part.byte_end > reference.byte_start


def _require_reference_cover(parts, reference):
    if parts[0].original_content is None:
        return
    if parts[0].byte_start > reference.byte_start or parts[-1].byte_end < reference.byte_end:
        raise ValueError("compile claim evidence lacks complete source part cover")
    require_native_unit(parts)


# What PyYAML refuses to read anywhere in a document: C0 controls other than tab
# and line breaks, DEL and C1 controls other than NEL, surrogates, U+FFFE/U+FFFF.
# One such character in a title made the page's whole frontmatter unreadable. See
# `docs/research/2026-09-14-one-page-cannot-close-the-vault.md`.
_YAML_REFUSED = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ud800-\udfff\ufffe\uffff]")


def escape_yaml(value: object) -> str:
    return (
        _YAML_REFUSED.sub("", str(value))
        .replace(chr(92), chr(92) + chr(92))
        .replace(chr(34), chr(92) + chr(34))
        .replace(chr(10), " ")
        .replace(chr(13), " ")
    )


def dailies_for_evidence(inputs: CompileInputs, date: str) -> list[DailySnapshot]:
    """Every part of that day the run carries.

    A long day is compiled in parts, and a part is a unit of *work*, not a
    boundary for evidence: the quoted line lives in exactly one of them. Asking
    for a single snapshot per date silently returned nothing as soon as a day
    was split, so no evidence from a long day could ever bind.
    """
    suffix = f"/{date}.md"
    return [item for item in inputs.dailies if item.logical_path.endswith(suffix)]


def resolve_captured_choice_bytes(reference, content, source_path):
    resolver = SOURCE_CHOICE_RESOLVER.get()
    if resolver is None:
        return EvidenceResolver(runtime_roots.vault_root()).resolve_bytes(reference, content, source_path=source_path)
    return resolver.resolve_bytes(reference, content, source_path=source_path, reuse_immutable=True)


# Every claim dropped in this process, so the compile can report the count
# where "ok" used to hide it (#28): in the state mirror and the changelog.
DROPPED_CLAIMS: list[dict[str, str]] = []


# Issue #26.2: `done` and `ok` said the same thing whether pages were published
# or only a candidate was quarantined. Each batch returns what it did.
QUARANTINE_OPERATION_PREFIX = "compile-quarantine:"
CANDIDATE_DIRECTORY = "knowledge/inbox/claims/"


@dataclass(frozen=True)
class BatchOutcome:
    """One batch's exit status and, when it committed, what the commit was."""

    status: int
    outcome: str | None = None
    paths: int = 0


def committed_outcome(result: CompileApplyResult) -> BatchOutcome:
    """Name what this batch did, in the words the operator needs (#26.2)."""
    paths = len(result.touched)
    if result.operation_id.startswith(QUARANTINE_OPERATION_PREFIX):
        print(
            f"compile_memory: batch quarantined: {paths} candidate(s) under "
            "knowledge/inbox/claims/, no page published; the daily stays "
            "pending until the candidate is reviewed."
        )
        return BatchOutcome(0, "quarantined", paths)
    candidates = sum(1 for path in result.touched if path.startswith(CANDIDATE_DIRECTORY))
    held = f"; {candidates} claim(s) quarantined under {CANDIDATE_DIRECTORY}" if candidates else ""
    print(f"compile_memory: batch published {paths - candidates} page(s){held}.")
    return BatchOutcome(0, "published", paths)


def compile_outcome(outcomes: Sequence[BatchOutcome]) -> str:
    """One word for the run: published, quarantined, partial, or nothing."""
    kinds = {item.outcome for item in outcomes if item.outcome}
    if not kinds:
        return "nothing"
    if len(kinds) == 1:
        return kinds.pop()
    return "partial"


def outcome_sentence(outcomes: Sequence[BatchOutcome]) -> str:
    counts: dict[str, list[int]] = {}
    for item in outcomes:
        if item.outcome:
            counts.setdefault(item.outcome, []).append(item.paths)
    parts = [
        f"{kind} {len(paths)} batch(es), {sum(paths)} path(s)"
        for kind, paths in sorted(counts.items())
    ]
    return "; ".join(parts) or "nothing to publish"


def report_dropped_claim(slug: str, detail: str) -> None:
    """A claim that cannot bind is dropped, never silently, and always counted."""
    DROPPED_CLAIMS.append({"slug": slug, "detail": detail[:MAX_FAILURE_DETAIL_CHARS]})
    print(
        f"compile_memory: claim dropped on {slug}: "
        f"{detail[:MAX_FAILURE_DETAIL_CHARS]}",
        file=sys.stderr,
    )
    _append_drop_record(slug, detail)


def _append_drop_record(slug: str, detail: str) -> None:
    """One JSON line per drop under logs/, best effort, never fatal."""

    day = datetime.now().strftime("%Y-%m-%d")
    record = {"at": datetime.now().isoformat(timespec="seconds"), "slug": slug, "detail": detail[:MAX_FAILURE_DETAIL_CHARS]}
    try:
        runtime_roots.reports_dir().mkdir(parents=True, exist_ok=True)
        with (runtime_roots.reports_dir() / f"compile-drops-{day}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def store_derived_claims(
    operation: dict[str, object], records: Sequence[object]
) -> None:
    """An operation with no surviving claim carries no `claims` key at all."""
    if not records:
        operation.pop("claims", None)
        return
    operation["claims"] = list(records)


class ReviewedClaimRecord(dict):
    """Transient citation ownership; the serialized ledger schema stays unchanged."""

    __slots__ = ("review_evidence_index", "review_evidence_digest")

    def __init__(self, record, index, item):
        super().__init__(record)
        self.review_evidence_index = index
        self.review_evidence_digest = sha256_bytes(canonical_json_bytes(item))


def verified_claim_quote(binding: Mapping[str, str], inputs: CompileInputs) -> str:
    """The exact span already verified by the shared evidence binder.

    The binder can widen a partial quote to its whole source line. Both the
    record text and its hash must refer to that same span, not the model's
    shorter original quote.
    """
    reference = EvidenceRef.parse(binding["reference"])
    source = reference_source(inputs, reference)
    return source.content[reference.byte_start:reference.byte_end].decode("utf-8", errors="strict")


def proposed_semantics(
    candidate: Mapping[str, object], date: str
) -> dict[str, object]:
    """Validity is the day the line was observed on, with no known end."""
    return {
        "subject": candidate["subject"],
        "relation": candidate["relation"],
        "value": candidate["value"],
        "qualifiers": candidate.get("qualifiers", []),
        "validity": {"from": date, "to": None},
    }


def claim_evidence_item(operation: Mapping[str, object], index: object) -> object:
    evidence = operation.get("evidence")
    if not isinstance(evidence, list) or not isinstance(index, int):
        raise ValueError("compile claim evidence index is invalid")
    if isinstance(index, bool) or not 0 <= index < len(evidence):
        raise ValueError("compile claim evidence index is out of range")
    return evidence[index]


def require_evidence_fields(item: object) -> tuple[str, str, str]:
    if isinstance(item, dict) and "native_event" in item:
        return _require_native_evidence_fields(item)
    return _require_legacy_evidence_fields(item)


def _require_legacy_evidence_fields(item):
    if not isinstance(item, dict) or set(item) != {
        "daily_date",
        "timestamp",
        "quoted_text",
        "claim",
    }:
        raise ValueError("compile evidence must be an object")
    date = item.get("daily_date")
    timestamp = item.get("timestamp")
    quote = item.get("quoted_text")
    if not _evidence_fields_valid(date, timestamp, quote, item.get("claim")):
        raise ValueError("compile evidence is incomplete")
    require_calendar_date(date)
    return date, timestamp, quote


def _require_native_evidence_fields(item):
    from llm_wiki.formats.schema import validate_schema_object

    validate_schema_object(item, NATIVE_EVIDENCE_SCHEMA)
    require_calendar_date(item["daily_date"])
    return item["daily_date"], item["timestamp"], item["quoted_text"]


def _evidence_fields_valid(
    date: object, timestamp: object, quote: object, claim: object
) -> bool:
    return (
        _evidence_matches(date, r"\d{4}-\d{2}-\d{2}")
        and _evidence_matches(timestamp, r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d")
        and _evidence_bounded_text(quote, CLAIM_RECORD_SCHEMA["properties"]["evidence"]["properties"]["text"]["maxLength"])
        and _evidence_single_line(claim, 1_000)
    )


def _evidence_matches(value: object, pattern: str) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(pattern, value) is not None


def _evidence_bounded_text(value: object, maximum: int) -> bool:
    if not isinstance(value, str):
        return False
    return 1 <= len(value) <= maximum


def _evidence_single_line(value: object, maximum: int) -> bool:
    if not _evidence_bounded_text(value, maximum):
        return False
    return "\r" not in value and "\n" not in value


def require_calendar_date(date: str) -> None:
    try:
        if calendar_date.fromisoformat(date).isoformat() != date:
            raise ValueError("noncanonical date")
    except ValueError as exc:
        raise ValueError("compile evidence date is invalid") from exc


def _source_content(source: object) -> bytes:
    if source is None:
        return b""
    return source.content


def declaring_entries(content: bytes, timestamp: str) -> list[tuple[int, int]]:
    resolver = SOURCE_CHOICE_RESOLVER.get()
    if type(resolver) is EvidenceResolver:
        return resolver.canonical_declaring_entries(content, timestamp)
    return [
        (start, end)
        for block_id, start, end in daily_entries(content)
        if block_id == timestamp
    ]


def quote_bearing(
    content: bytes, matched: list[tuple[int, int]], quote_bytes: bytes
) -> list[tuple[int, int]]:
    """Of the entries a timestamp names, those holding the quote exactly once.

    Without a quote there is nothing to settle the address with, so the
    candidates are returned untouched and the caller refuses them as ambiguous.
    """
    if not quote_bytes:
        return matched
    return [
        (start, end)
        for start, end in matched
        if _single_quote_in_range(content, quote_bytes, start, end)
    ]


def _single_quote_in_range(content, quote, start, end):
    found = content.find(quote, start, end)
    return found >= 0 and content.find(quote, found + len(quote), end) < 0


def evidence_block(
    source: object, timestamp: str, quote_bytes: bytes = b""
) -> tuple[bytes, int]:
    """The entry this evidence belongs to, as bytes plus its offset in the source.

    Entries are delimited by `evidence_resolver.daily_entries`, the one
    definition. The timestamp selects the candidates; when several entries
    declare it, the quote settles which one — the address is fragile, the quote
    is the proof, and a daily log is append-only, so twelve entries written in
    one second stay that way. Zero candidates, or a quote that no single
    candidate holds exactly once, are refused as before. See
    knowledge/notes/daily-entry-quote-anchor-decision.md.
    """
    content = _source_content(source)
    if isinstance(source, DailySnapshot) and source.original_content is not None:
        return _continuation_block(source, timestamp, quote_bytes)
    return _entry_block(content, timestamp, quote_bytes)


def _entry_block(content: bytes, timestamp: str, quote_bytes: bytes) -> tuple[bytes, int]:
    declared = declaring_entries(content, timestamp)
    matched = declared
    if len(matched) > 1:
        matched = quote_bearing(content, matched, quote_bytes)
    if len(matched) != 1:
        raise ValueError(ambiguous_block_message(timestamp, declared, matched))
    start, end = matched[0]
    return content[start:end], start


def _continuation_block(
    source: DailySnapshot, timestamp: str, quote_bytes: bytes
) -> tuple[bytes, int]:
    content = source.original_content
    declared = _original_declaring_entries(source, timestamp)
    spans = _selected_entry_spans(source, declared)
    matched = spans
    if len(spans) > 1:
        matched = quote_bearing(content, spans, quote_bytes)
    if len(matched) != 1:
        raise ValueError(ambiguous_block_message(timestamp, declared, matched))
    start, end = matched[0]
    return content[start:end], start


def _selected_entry_spans(
    source: DailySnapshot, declared: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    return [
        (max(start, source.byte_start), min(end, source.byte_end))
        for start, end in declared if start < source.byte_end and end > source.byte_start
    ]


def _original_declaring_entries(
    source: DailySnapshot, timestamp: str
) -> list[tuple[int, int]]:
    resolver = SOURCE_CHOICE_RESOLVER.get()
    if resolver is not None:
        return resolver.declaring_entries(source.original_content, source.original_entries, timestamp)
    return [(start, end) for block_id, start, end in source.original_entries if block_id == timestamp]


def ambiguous_block_message(
    timestamp: str, declared: list[tuple[int, int]], matched: list[tuple[int, int]]
) -> str:
    """Say which of the two failures happened; the class alone taught nobody."""
    return (
        "compile evidence timestamp block is ambiguous or missing: "
        f"timestamp {timestamp!r} declared by {len(declared)} entr(y/ies), "
        f"quote found in {len(matched)} of them"
    )


def sole_quote_offset(block: bytes, quote_bytes: bytes) -> int:
    """An ambiguous quote is refused: one entry must name one span."""
    offset = block.find(quote_bytes)
    if offset < 0 or block.find(quote_bytes, offset + max(len(quote_bytes), 1)) >= 0:
        raise ValueError("compile evidence does not match the immutable snapshot")
    return offset


def line_bounds(block: bytes, quote_offset: int, quote_length: int) -> tuple[int, int]:
    line_start = block.rfind(b"\n", 0, quote_offset) + 1
    line_end = block.find(b"\n", quote_offset + quote_length)
    if line_end < 0:
        line_end = len(block)
    return line_start, line_end


def completed_line(
    block: bytes, quote_offset: int, quote_bytes: bytes, quote: str
) -> tuple[str, bytes, int]:
    """The quote as one whole line, so half a sentence cannot be cited.

    A quote that is part of one line is widened to that line rather than
    dropped: the anchor is still exact bytes of the immutable source, only the
    whole line of them. Issue #28 counted sixteen claims dropped in one compile
    for quoting less than a line, each fact lost for good, with the compile
    reporting ok.
    """
    line_start, line_end = line_bounds(block, quote_offset, len(quote_bytes))
    source_line = block[line_start:line_end].decode("utf-8", errors="strict").strip()
    whole = without_bullet(source_line)
    if quote == whole:
        return quote, quote_bytes, quote_offset
    whole_bytes = whole.encode("utf-8")
    whole_offset = block.find(whole_bytes, line_start, line_end)
    if whole_offset < 0:
        raise ValueError(
            "compile evidence must quote one complete source line: "
            f"quoted {quote[:MAX_QUOTE_REPORT_CHARS]!r}, line {source_line[:MAX_QUOTE_REPORT_CHARS]!r}"
        )
    _report_widened_quote(quote, whole)
    return whole, whole_bytes, whole_offset


# What a dropped or widened quote's report shows of the text: enough to find
# the line, never the whole entry.
MAX_QUOTE_REPORT_CHARS = 160


def _report_widened_quote(quote: str, whole: str) -> None:
    print(
        "compile_memory: claim quote widened to its line: "
        f"{quote[:MAX_QUOTE_REPORT_CHARS]!r} -> {whole[:MAX_QUOTE_REPORT_CHARS]!r}",
        file=sys.stderr,
    )


def without_bullet(source_line: str) -> str:
    source_line = source_line.strip()
    bullet = re.match(r"^(?:[-+*]|\d+[.)])\s+(.*)$", source_line)
    if bullet is None:
        return source_line.strip()
    return bullet.group(1).strip()


def require_claims(operation: Mapping[str, object], inputs: CompileInputs) -> None:
    claims = operation.get("claims", [])
    if not isinstance(claims, list) or len(claims) > 100:
        raise ValueError("compile operation claims must be a bounded array")
    _require_unique_claim_ids(claims)
    for record in claims:
        require_claim_evidence(record, inputs)


def _require_unique_claim_ids(claims: list[object]) -> None:
    claim_ids = [
        str(record.get("id", "")) for record in claims if isinstance(record, Mapping)
    ]
    if len(claim_ids) != len(claims) or len(claim_ids) != len(set(claim_ids)):
        raise ValueError("compile operation contains a duplicate claim id")


def require_claim_evidence(record: object, inputs: CompileInputs) -> None:
    validate_claim_record(record)
    assert isinstance(record, Mapping)
    if record.get("lifecycle") != "active":
        raise ValueError("compile input claims must be active")
    claim_evidence = record["evidence"]
    assert isinstance(claim_evidence, Mapping)
    _require_resolved_claim_evidence(claim_evidence, inputs)


def _require_resolved_claim_evidence(
    claim_evidence: Mapping[str, object], inputs: CompileInputs
) -> None:
    reference = EvidenceRef.parse(claim_evidence["reference"])
    source = reference_source(inputs, reference)
    resolved = resolve_captured_choice_bytes(
        reference,
        source.content,
        runtime_roots.vault_root() / source.logical_path,
    )
    _require_literal_match(resolved, claim_evidence)


def _require_literal_match(
    resolved: object, claim_evidence: Mapping[str, object]
) -> None:
    if (
        resolved.sha256 != claim_evidence["sha256"]
        or resolved.bytes.decode("utf-8", errors="strict") != claim_evidence["text"]
    ):
        raise ValueError("compile claim literal evidence does not match")


def project_line(operation: Mapping[str, object]) -> str:
    project = operation.get("project")
    return f"project: {project}\n" if isinstance(project, str) else ""


def render_page(
    operation: dict[str, object], completed_at: str, evidence_refs: Sequence[str] = ()
) -> bytes:
    category = str(operation["category"])
    title = str(operation["title"])
    summary = str(operation["summary"])
    body_section = str(operation.get("body_section") or "Lesson")
    evidence = operation["evidence"]
    assert isinstance(evidence, list)
    text = (
        "---\n"
        f"type: {CATEGORY_SINGULAR[category]}\n"
        f'title: "{escape_yaml(title)}"\n'
        f'description: "{escape_yaml(summary)}"\n'
        f"timestamp: {completed_at}\n"
        "confidence: medium\n"
        "source_authority: ai-derived\n"
        f"{project_line(operation)}"
        "---\n\n"
        f"# {title}\n\n"
        f"One-sentence summary: {summary}\n\n"
        f"## {body_section}\n{operation['body_markdown']}\n\n"
        "## Evidence\n"
        + evidence_lines(evidence, evidence_refs)
        + _related_section(operation.get("related"))
        + "\n"
    )
    return text.encode("utf-8")


def evidence_lines(
    evidence: Sequence[object], evidence_refs: Sequence[str]
) -> str:
    if len(evidence_refs) != len(evidence):
        raise ValueError("compiled evidence references do not match evidence entries")
    return "\n".join(
        f"- `{reference}` — {item.get('claim', '')}"
        for item, reference in zip(evidence, evidence_refs)
    )


def _related_section(related: object) -> str:
    if not isinstance(related, list) or not related:
        return ""
    return "\n\n## Related\n" + "\n".join(f"- {item}" for item in related)


def _ledger_bytes(claims: list, *, previous_version=None) -> bytes:
    return claim_json_bytes(claim_ledger_document(claims, previous_version=previous_version))


def _merged_claims(existing: list, additions: list) -> list:
    """Existing claims plus the new ones; a claim the page already holds is kept once.

    The id is the date and the semantic fingerprint, so a day compiled again brings
    its claims again under the same ids; the page's copy stays, as a repeat inside
    one run is dropped. One id with another fingerprint is a real conflict.
    """
    by_id = {str(item["id"]): item for item in existing}
    if len(by_id) != len(existing):
        raise ValueError("target ledger contains a duplicate claim id")
    for record in additions:
        _admit_claim(by_id, record)
    return list(by_id.values())


def _admit_claim(by_id: dict, record: Mapping[str, object]) -> None:
    known = by_id.setdefault(str(record["id"]), record)
    if known.get("fingerprint") != record.get("fingerprint"):
        raise ValueError("compile claim id already exists in target ledger")


def with_claim_ledger(page: bytes, records: Sequence[Mapping[str, object]]) -> bytes:
    from claims import parse_claim_ledger

    if not records:
        return page
    existing = parse_claim_ledger(page)
    additions = [json.loads(claim_json_bytes(item)) for item in records]
    match = claim_ledger_match(page)
    if existing is None:
        opening = b"\n\n## Claims\n```json\n"
        return page.rstrip() + opening + _ledger_bytes(additions) + b"\n```\n"
    merged = _ledger_bytes(_merged_claims(existing["claims"], additions),
                           previous_version=existing["schema_version"])
    return page[: match.start(2)] + merged + page[match.end(2) :]


def append_log_bytes(content: bytes, entry: str) -> bytes:
    """Append new editorial bytes without changing any authenticated history."""
    content.decode("utf-8")
    return content + (entry.rstrip() + "\n").encode("utf-8")
