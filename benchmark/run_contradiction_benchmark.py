"""Generate and execute the frozen deterministic contradiction benchmark."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from contradiction_pipeline import run_frozen_benchmark  # noqa: E402
from reliable_memory import (  # noqa: E402
    canonical_json_bytes,
    sha256_bytes,
    validate_schema,
)

SCHEMA = ROOT / "benchmark/contradiction-v1.schema.json"
CATEGORIES = [
    "equality",
    "interval",
    "functional-relation",
    "authority-lifecycle",
    "keep-both-refine",
    "quarantine",
]


def _semantic(
    case_id: str,
    value: str,
    *,
    relation: str = "has-state",
    start: str | None = "2026-01-01",
    end: str | None = None,
) -> dict[str, object]:
    return {
        "subject": f"subject-{case_id}",
        "relation": relation,
        "value": {"type": "string", "value": value},
        "qualifiers": [],
        "validity": {"from": start, "to": end},
    }


def _record(
    claim_id: str,
    semantic: dict[str, object],
    text: str,
    authority: str,
    reference: str,
    block_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    evidence = {
        "reference": reference,
        "sha256": sha256_bytes(text.encode()),
        "text": text,
    }
    extraction = {
        "id": claim_id,
        "text": text,
        **semantic,
        "lifecycle": "active",
        "confidence": "high",
        "authority": authority,
        "evidence": evidence,
        "links": [],
        "extractor_version": "benchmark/v1",
    }
    normalized = {
        "schema_version": "claim/v1",
        "id": claim_id,
        "fingerprint": sha256_bytes(canonical_json_bytes(semantic)),
        "text": text,
        **semantic,
        "observed_at": f"2026-01-01T{block_id}Z",
        "lifecycle": "active",
        "confidence": "high",
        "authority": authority,
        "evidence": evidence,
        "links": [],
        "extractor_version": "benchmark/v1",
    }
    return extraction, normalized


class _CaseShape(NamedTuple):
    old_semantic: dict
    new_semantic: dict
    old_authority: str
    new_authority: str
    expected_class: str
    expected_lifecycle: str
    negative: bool


def _default_case(case_id: str) -> _CaseShape:
    return _CaseShape(
        _semantic(case_id, "blue"), _semantic(case_id, "blue"), "web", "user", "equivalent", "keep-both", True
    )


def _interval_case(case_id: str) -> _CaseShape:
    return _CaseShape(
        _semantic(case_id, "blue", start="2026-01-01", end="2026-02-01"),
        _semantic(case_id, "red", start="2026-02-01"),
        "web",
        "user",
        "temporal-distinct",
        "keep-both",
        True,
    )


def _functional_relation_case(case_id: str) -> _CaseShape:
    return _CaseShape(
        _semantic(case_id, "blue"), _semantic(case_id, "red"), "web", "user", "contradiction", "supersede", False
    )


def _authority_lifecycle_case(case_id: str) -> _CaseShape:
    return _CaseShape(
        _semantic(case_id, "blue"), _semantic(case_id, "red"), "user", "inferred", "contradiction", "quarantine", True
    )


def _keep_both_refine_case(case_id: str, index: int) -> _CaseShape:
    if index < 20:
        return _CaseShape(
            _semantic(case_id, "blue", relation="uses"),
            _semantic(case_id, "red", relation="uses"),
            "web",
            "user",
            "compatible",
            "keep-both",
            True,
        )
    return _CaseShape(
        _semantic(case_id, "blue", start=None),
        _semantic(case_id, "blue", start="2026-02-01"),
        "web",
        "user",
        "refinement",
        "refine",
        True,
    )


def _quarantine_case(case_id: str) -> _CaseShape:
    return _CaseShape(
        _semantic(case_id, "blue", relation="depends-on"),
        _semantic(case_id, "blue", relation="uses"),
        "web",
        "user",
        "compatible",
        "quarantine",
        True,
    )


_CASE_BUILDERS = {
    "interval": _interval_case,
    "functional-relation": _functional_relation_case,
    "authority-lifecycle": _authority_lifecycle_case,
    "quarantine": _quarantine_case,
}


def _case_shape(category: str, index: int, case_id: str) -> _CaseShape:
    if category == "keep-both-refine":
        return _keep_both_refine_case(case_id, index)
    return _CASE_BUILDERS.get(category, _default_case)(case_id)


class _CaseSpec(NamedTuple):
    case_id: str
    category: str
    block_id: str
    shape: _CaseShape
    old_text: str
    new_text: str


def _specification(category: str, index: int, ordinal: int) -> _CaseSpec:
    case_id = f"{category}-{index:02d}"
    shape = _case_shape(category, index, case_id)
    hour, minute = divmod(ordinal, 60)
    old = shape.old_semantic
    new = shape.new_semantic
    return _CaseSpec(
        case_id,
        category,
        f"{hour:02d}:{minute:02d}:00",
        shape,
        f"old {case_id}: {old['relation']} {old['value']['value']}",
        f"new {case_id}: {new['relation']} {new['value']['value']}",
    )


def _specifications() -> list[_CaseSpec]:
    specifications: list[_CaseSpec] = []
    ordinal = 0
    for category in CATEGORIES:
        for index in range(40):
            specifications.append(_specification(category, index, ordinal))
            ordinal += 1
    return specifications


def _source_text(specifications: list[_CaseSpec]) -> str:
    blocks = ["# 2026-01-01\n"]
    for spec in specifications:
        blocks.append(f"## [{spec.block_id}] benchmark\n{spec.old_text}\n{spec.new_text}\n")
    return "".join(blocks)


def _reference(source_bytes: bytes, source_digest: str, block_id: str, text: str) -> str:
    start = source_bytes.index(text.encode())
    return (
        f"daily:2026-01-01 sha256:{source_digest} block:{block_id} "
        f"bytes:{start}-{start + len(text.encode())}"
    )


def _case(spec: _CaseSpec, source_bytes: bytes, source_digest: str) -> dict[str, object]:
    shape = spec.shape
    new_extraction, expected_new = _record(
        f"new-{spec.case_id}",
        shape.new_semantic,
        spec.new_text,
        shape.new_authority,
        _reference(source_bytes, source_digest, spec.block_id, spec.new_text),
        spec.block_id,
    )
    _old_extraction, existing = _record(
        f"old-{spec.case_id}",
        shape.old_semantic,
        spec.old_text,
        shape.old_authority,
        _reference(source_bytes, source_digest, spec.block_id, spec.old_text),
        spec.block_id,
    )
    return {
        "id": spec.case_id,
        "category": spec.category,
        "block_id": spec.block_id,
        "new_extraction": new_extraction,
        "expected_new_claim": expected_new,
        "existing_claim": existing,
        "existing_page": f"knowledge/notes/{spec.case_id}.md",
        "retrievable": True,
        "expected_class": shape.expected_class,
        "expected_lifecycle": shape.expected_lifecycle,
        "expected_provenance_valid": True,
        "negative_control": shape.negative,
    }


def build_corpus() -> dict[str, object]:
    specifications = _specifications()
    source = _source_text(specifications)
    source_bytes = source.encode()
    source_digest = sha256_bytes(source_bytes)
    return {
        "version": "contradiction-v1",
        "provider": "fake",
        "categories": CATEGORIES,
        "relation_metadata": {
            "functional": [
                "equals",
                "has-state",
                "has-value",
                "located-at",
                "starts-at",
                "ends-at",
            ],
            "non_functional": ["member-of", "uses", "depends-on"],
        },
        "source": source,
        "cases": [_case(spec, source_bytes, source_digest) for spec in specifications],
    }


def _write_corpus(path: Path) -> None:
    corpus = build_corpus()
    validate_schema(corpus, SCHEMA)
    path.write_bytes(canonical_json_bytes(corpus) + b"\n")


def _load_canonical_corpus(path: Path) -> dict:
    raw = path.read_bytes()
    corpus = json.loads(raw)
    validate_schema(corpus, SCHEMA)
    if canonical_json_bytes(corpus) + b"\n" != raw:
        raise ValueError("corpus is not restricted canonical JSON")
    return corpus


def _gates_pass(metrics) -> bool:
    exact = (
        metrics.extraction_f1,
        metrics.candidate_recall,
        metrics.class_macro_f1,
        metrics.lifecycle_macro_f1,
        metrics.provenance_correctness,
    )
    if any(value != 1 for value in exact):
        return False
    return metrics.quarantine_risk == 0 and metrics.false_supersession <= 0.01


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / "benchmark/contradiction-v1.json"
    )
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    if args.generate:
        _write_corpus(args.corpus)
        return 0
    metrics = run_frozen_benchmark(_load_canonical_corpus(args.corpus))
    print(json.dumps(metrics.canonical(), sort_keys=True, indent=2))
    return 0 if _gates_pass(metrics) else 1


if __name__ == "__main__":
    raise SystemExit(main())
