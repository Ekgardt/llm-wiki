"""Helpers shared by test_evidence_resolver.py and the test files importing them."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reference(daily_id: str, source: bytes, block: str, start: int, end: int) -> str:
    return (
        f"daily:{daily_id} sha256:{_sha(source)} block:{block} "
        f"bytes:{start}-{end}"
    )


def _archive_source_receipt(root: Path, logical_path: str, source: bytes) -> Path:
    import compile_pipeline
    from llm_wiki import (
        compile_pipeline_receipts,
        compile_pipeline_sources,
    )

    identity = compile_pipeline.compile_source_identity(logical_path, _sha(source))
    legacy = root / f"knowledge/daily/receipts/v3-{identity}.md"
    parts = compile_pipeline_sources.daily_parts(logical_path, source)
    if len(parts) != 1:
        return legacy
    descriptor = compile_pipeline_receipts.v4_source_descriptor(parts[0])
    current = root / "knowledge/daily/receipts" / compile_pipeline_receipts.context_receipt_path(descriptor).name
    if current.exists():
        return current
    return legacy


def _archive_receipt_authority(path: Path, fallback_identity: str) -> tuple[str, str, str]:
    if not path.exists():
        return "a" * 64, fallback_identity, "compile:test"
    raw = path.read_bytes()
    record = json.loads(raw.split(b"```json\n", 1)[1].split(b"\n```", 1)[0])
    return _sha(raw), record["source_identity"], record["operation_id"]


def _write_bag(root: Path, daily_id: str, source: bytes, *, suffix: str = "one") -> Path:
    bag = root / "knowledge" / "daily" / "archive" / daily_id[:7] / f"bag-test-{suffix}"
    payload_name = f"data/{daily_id}.md"
    (bag / "data").mkdir(parents=True)
    (bag / payload_name).write_bytes(source)
    (bag / "bagit.txt").write_bytes(
        b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
    )
    (bag / "bag-info.txt").write_bytes(
        (
            f"Bagging-Date: 2026-07-14\n"
            f"Payload-Oxum: {len(source)}.1\n"
            f"External-Identifier: daily:{daily_id}\n"
        ).encode()
    )
    (bag / "manifest-sha256.txt").write_bytes(
        f"{_sha(source)}  {payload_name}\n".encode()
    )
    block_start = source.index(b"## [")
    evidence = {
        "block_id": "evt-1",
        "byte_start": block_start,
        "byte_end": len(source),
        "line_start": 2,
        "line_end": 4,
        "sha256": _sha(source[block_start:]),
    }
    import compile_pipeline

    logical_path = f"knowledge/daily/{daily_id}.md"
    source_identity = compile_pipeline.compile_source_identity(
        logical_path, _sha(source)
    )
    receipt_path = _archive_source_receipt(root, logical_path, source)
    receipt_hash, source_identity, operation_id = _archive_receipt_authority(receipt_path, source_identity)
    manifest = {
        "schema_version": "archive-manifest/v1",
        "logical_daily_id": daily_id,
        "original_path": f"knowledge/daily/{daily_id}.md",
        "source_hash": _sha(source),
        "payload_hash": _sha(source),
        "compile_receipt_ref": {
            "schema": "compile-receipt-ref/v1",
            "path": receipt_path.relative_to(root).as_posix(),
            "logical_path": logical_path,
            "source_digest": _sha(source),
            "source_identity": source_identity,
            "receipt_file_hash": receipt_hash,
        },
        "queue_preflight": {
            "checked_at": "2026-07-14T00:00:00Z",
            "passed": True,
            "blocking_task_ids": [],
        },
        "operations": [{"operation_id": operation_id, "state": "succeeded"}],
        "evidence": [evidence],
        "pins": [],
        "retention_days": 90,
    }
    (bag / "archive-manifest.json").write_bytes(canonical_json_bytes(manifest))
    tags = ("archive-manifest.json", "bag-info.txt", "bagit.txt", "manifest-sha256.txt")
    (bag / "tagmanifest-sha256.txt").write_bytes(
        "".join(f"{_sha((bag / name).read_bytes())}  {name}\n" for name in tags).encode()
    )
    return bag


def _authorize_archive_source(
    root: Path, source: bytes, monkeypatch: pytest.MonkeyPatch
) -> Path:
    from llm_wiki import (
        compile_pipeline_apply,
        compile_pipeline_batches,
        compile_pipeline_sources,
    )
    from markdown_transaction import MarkdownCoordinator

    state_root = root / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "knowledge/index.md").write_bytes(b"# index\n")
    (root / "knowledge/log.local.md").write_bytes(b"# log\n")
    (root / "AGENTS.md").write_bytes(b"contract\n")
    daily = root / "knowledge/daily/2026-01-01.md"
    daily.write_bytes(source)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model=None)[0]
    compile_pipeline_apply.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="c" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T00:00:00Z",
        batch=batch,
        provider_budget={
            "provider": "fake",
            "model": "fake-v1",
            "max_output_tokens": 4000,
        },
    )
    daily.unlink()
    return state_root


def _long_day(entries: int, *, filler: int = 400) -> bytes:
    """A day the compiler must split: many entries, each one marker plus a heading."""
    parts = []
    for index in range(entries):
        parts.append(
            f"<!-- llm-wiki-operation: op-{index} -->\n"
            f"## [evt-{index}] entry {index}\n"
            f"quote-{index} " + "x" * filler + "\n"
        )
    return "".join(parts).encode()
