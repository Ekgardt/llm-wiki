"""Generate and execute the frozen deterministic contradiction benchmark."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def build_corpus() -> dict[str, object]:
    specifications = []
    blocks = ["# 2026-01-01\n"]
    ordinal = 0
    for category in CATEGORIES:
        for index in range(40):
            case_id = f"{category}-{index:02d}"
            old_semantic = _semantic(case_id, "blue")
            new_semantic = _semantic(case_id, "blue")
            old_authority, new_authority = "web", "user"
            expected_class, expected_lifecycle = "equivalent", "keep-both"
            negative = True
            if category == "interval":
                old_semantic = _semantic(
                    case_id, "blue", start="2026-01-01", end="2026-02-01"
                )
                new_semantic = _semantic(case_id, "red", start="2026-02-01")
                expected_class = "temporal-distinct"
            elif category == "functional-relation":
                new_semantic = _semantic(case_id, "red")
                expected_class, expected_lifecycle, negative = (
                    "contradiction",
                    "supersede",
                    False,
                )
            elif category == "authority-lifecycle":
                old_authority, new_authority = "user", "inferred"
                new_semantic = _semantic(case_id, "red")
                expected_class, expected_lifecycle = "contradiction", "quarantine"
            elif category == "keep-both-refine":
                if index < 20:
                    old_semantic = _semantic(case_id, "blue", relation="uses")
                    new_semantic = _semantic(case_id, "red", relation="uses")
                    expected_class = "compatible"
                else:
                    old_semantic = _semantic(case_id, "blue", start=None)
                    new_semantic = _semantic(case_id, "blue", start="2026-02-01")
                    expected_class, expected_lifecycle = "refinement", "refine"
            elif category == "quarantine":
                old_semantic = _semantic(case_id, "blue", relation="depends-on")
                new_semantic = _semantic(case_id, "blue", relation="uses")
                expected_class, expected_lifecycle = "compatible", "quarantine"
            hour, minute = divmod(ordinal, 60)
            block_id = f"{hour:02d}:{minute:02d}:00"
            old_text = f"old {case_id}: {old_semantic['relation']} {old_semantic['value']['value']}"
            new_text = f"new {case_id}: {new_semantic['relation']} {new_semantic['value']['value']}"
            blocks.append(
                f"## [{block_id}] benchmark\n{old_text}\n{new_text}\n"
            )
            specifications.append(
                (
                    case_id,
                    category,
                    block_id,
                    old_semantic,
                    new_semantic,
                    old_authority,
                    new_authority,
                    old_text,
                    new_text,
                    expected_class,
                    expected_lifecycle,
                    negative,
                )
            )
            ordinal += 1
    source = "".join(blocks)
    source_bytes = source.encode()
    source_digest = sha256_bytes(source_bytes)
    cases = []
    for (
        case_id,
        category,
        block_id,
        old_semantic,
        new_semantic,
        old_authority,
        new_authority,
        old_text,
        new_text,
        expected_class,
        expected_lifecycle,
        negative,
    ) in specifications:
        def reference(text: str) -> str:
            start = source_bytes.index(text.encode())
            return (
                f"daily:2026-01-01 sha256:{source_digest} block:{block_id} "
                f"bytes:{start}-{start + len(text.encode())}"
            )

        new_extraction, expected_new = _record(
            f"new-{case_id}",
            new_semantic,
            new_text,
            new_authority,
            reference(new_text),
            block_id,
        )
        _old_extraction, existing = _record(
            f"old-{case_id}",
            old_semantic,
            old_text,
            old_authority,
            reference(old_text),
            block_id,
        )
        cases.append(
            {
                "id": case_id,
                "category": category,
                "block_id": block_id,
                "new_extraction": new_extraction,
                "expected_new_claim": expected_new,
                "existing_claim": existing,
                "existing_page": f"knowledge/notes/{case_id}.md",
                "retrievable": True,
                "expected_class": expected_class,
                "expected_lifecycle": expected_lifecycle,
                "expected_provenance_valid": True,
                "negative_control": negative,
            }
        )
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
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus", type=Path, default=ROOT / "benchmark/contradiction-v1.json"
    )
    parser.add_argument("--generate", action="store_true")
    args = parser.parse_args()
    if args.generate:
        corpus = build_corpus()
        validate_schema(corpus, SCHEMA)
        args.corpus.write_bytes(canonical_json_bytes(corpus) + b"\n")
        return 0
    raw = args.corpus.read_bytes()
    corpus = json.loads(raw)
    validate_schema(corpus, SCHEMA)
    if canonical_json_bytes(corpus) + b"\n" != raw:
        raise ValueError("corpus is not restricted canonical JSON")
    metrics = run_frozen_benchmark(corpus)
    print(json.dumps(metrics.canonical(), sort_keys=True, indent=2))
    gates = (
        metrics.extraction_f1 == 1
        and metrics.candidate_recall == 1
        and metrics.class_macro_f1 == 1
        and metrics.lifecycle_macro_f1 == 1
        and metrics.provenance_correctness == 1
        and metrics.quarantine_risk == 0
        and metrics.false_supersession <= 0.01
    )
    return 0 if gates else 1


if __name__ == "__main__":
    raise SystemExit(main())
