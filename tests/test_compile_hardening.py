from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import MappingProxyType

import pytest
from compile_cache import CompileCache
from llm_client import LLMResult, ProviderDescriptor
from markdown_transaction import MarkdownCoordinator, TransactionFailure
from reliable_memory import canonical_json_bytes, sha256_bytes


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Index\n")
    (root / "knowledge/log.md").write_bytes(b"# Log\n")
    (root / "AGENTS.md").write_bytes(b"contract\n")
    import compile_memory

    monkeypatch.setattr(compile_memory, "ROOT", root)
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(compile_memory, "MEMORY", root / "knowledge")
    monkeypatch.setattr(compile_memory, "DAILY_DIR", root / "knowledge/daily")
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", root / "knowledge/notes")
    monkeypatch.setattr(compile_memory, "INDEX", root / "knowledge/index.md")
    monkeypatch.setattr(compile_memory, "LOG", root / "knowledge/log.md")
    monkeypatch.setattr(compile_memory, "AGENTS", root / "AGENTS.md")
    return root, state_root


def _daily(root: Path, name: str = "2026-07-14.md", quote: str = "durable fact") -> Path:
    path = root / "knowledge/daily" / name
    path.write_text(f"## [10:00:00] event\n{quote}\n", encoding="utf-8")
    return path


def _semantic(*, action: str = "create", slug: str = "safe-note") -> dict[str, object]:
    return {
        "action": action,
        "category": "patterns",
        "slug": slug,
        "title": "Safe Note",
        "summary": "A bounded summary.",
        "body_section": "Lesson",
        "body_markdown": "A bounded body.",
        "evidence": [
            {
                "daily_date": "2026-07-14",
                "timestamp": "10:00:00",
                "quoted_text": "durable fact",
                "claim": "Supports the note.",
            }
        ],
        "related": [],
    }


def _plan(*, action: str = "create", slug: str = "safe-note") -> dict[str, object]:
    semantic = _semantic(action=action, slug=slug)
    return {
        "schema_version": "compile-plan/v2",
        "operations": [
            {
                "kind": "create" if action == "create" else "replace",
                "path": f"knowledge/notes/{slug}.md",
                "content": canonical_json_bytes(semantic).decode(),
            }
        ],
    }


def _provider(name: str, index: int = 0) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider=name,
        model=f"{name}-model",
        capabilities=MappingProxyType(
            {"structured_output": "native", "max_tokens_enforced": True}
        ),
        inference_settings=MappingProxyType({"max_tokens": 4000}),
        candidate_index=index,
        fallback_from=(),
    )


def _compile(root: Path, state_root: Path, daily: Path):
    import compile_memory

    coordinator = MarkdownCoordinator(root, state_root)
    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    result = compile_memory.apply_compile_plan(
        inputs,
        _plan(),
        action_key="a" * 64,
        trigger="manual",
        coordinator=coordinator,
        completed_at="2026-07-14T12:00:00Z",
        batch=batch,
        provider_budget={
            "provider": "fake",
            "model": "fake-v1",
            "max_output_tokens": 4000,
        },
    )
    return coordinator, inputs, result


def test_all_filters_committed_receipts_and_keeps_uncompiled_mixed_source(vault):
    root, state_root = vault
    first = _daily(root)
    coordinator, _inputs, _result = _compile(root, state_root, first)
    second = _daily(root, "2026-07-15.md", "other")
    import compile_memory

    selected = compile_memory.select_dailies(
        Namespace(file=None, all=True), {}, coordinator=coordinator
    )

    assert selected == [second]


def test_corrupt_receipt_is_an_error_not_pending(vault):
    root, state_root = vault
    daily = _daily(root)
    digest = sha256_bytes(daily.read_bytes())
    import compile_memory

    identity = compile_memory.compile_source_identity(
        f"knowledge/daily/{daily.name}", digest
    )
    (root / f"knowledge/daily/receipts/v3-{identity}.md").write_text(
        "not a receipt", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="receipt"):
        compile_memory.select_dailies(
            Namespace(file=None, all=True), {},
            coordinator=MarkdownCoordinator(root, state_root),
        )


def test_native_semantic_schema_is_closed_through_evidence_fields():
    import compile_memory

    operation = compile_memory.RAW_PLAN_SCHEMA["properties"]["operations"]["items"]
    evidence = operation["properties"]["evidence"]["items"]
    assert operation["additionalProperties"] is False
    assert evidence["additionalProperties"] is False
    assert operation["properties"]["action"]["enum"] == ["create", "update"]
    assert operation["properties"]["category"]["enum"] == sorted(
        compile_memory.ALLOWED_CATEGORIES
    )


def test_target_created_after_snapshot_conflicts_instead_of_becoming_update(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    target = root / "knowledge/notes/safe-note.md"
    target.write_bytes(b"concurrent\n")

    with pytest.raises((FileExistsError, TransactionFailure, ValueError)):
        compile_memory.apply_compile_plan(
            inputs,
            _plan(),
            action_key="b" * 64,
            trigger="manual",
            coordinator=MarkdownCoordinator(root, state_root),
        )
    assert target.read_bytes() == b"concurrent\n"
    assert not list((root / "knowledge/daily/receipts").glob("*.md"))


def test_target_changed_after_snapshot_conflicts_with_frozen_update(vault):
    root, state_root = vault
    daily = _daily(root)
    target = root / "knowledge/notes/safe-note.md"
    target.write_bytes(b"before\n")
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    target.write_bytes(b"concurrent\n")

    with pytest.raises((TransactionFailure, ValueError)):
        compile_memory.apply_compile_plan(
            inputs,
            _plan(action="update"),
            action_key="c" * 64,
            trigger="manual",
            coordinator=MarkdownCoordinator(root, state_root),
        )
    assert target.read_bytes() == b"concurrent\n"


def test_receipt_copy_is_rejected_without_matching_committed_transaction(vault):
    root, state_root = vault
    daily = _daily(root)
    coordinator, inputs, _result = _compile(root, state_root, daily)
    import compile_memory

    source = inputs.dailies[0]
    identity = compile_memory.compile_source_identity(
        source.logical_path, source.sha256
    )
    receipt = root / f"knowledge/daily/receipts/v3-{identity}.md"
    forged_path = "knowledge/daily/2099-01-01.md"
    forged_identity = compile_memory.compile_source_identity(
        forged_path, source.sha256
    )
    forged = root / f"knowledge/daily/receipts/v3-{forged_identity}.md"
    forged.write_bytes(receipt.read_bytes())

    with pytest.raises(ValueError, match="receipt"):
        compile_memory.read_compile_receipt_v3(
            forged_path, source.sha256, coordinator
        )


def test_snapshot_rejects_symlinked_and_oversized_sources(vault, monkeypatch):
    root, _state_root = vault
    daily = _daily(root)
    link = root / "AGENTS.md"
    actual = root / "actual.md"
    actual.write_text("contract", encoding="utf-8")
    link.unlink()
    try:
        link.symlink_to(actual)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    import compile_memory

    with pytest.raises((PermissionError, ValueError), match="symlink|regular"):
        compile_memory.snapshot_compile_inputs([daily])


def test_snapshot_and_provider_responses_enforce_byte_caps(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    monkeypatch.setattr(compile_memory, "MAX_SOURCE_BYTES", 8)
    with pytest.raises(ValueError, match="exceeds"):
        compile_memory.snapshot_compile_inputs([daily])

    monkeypatch.setattr(compile_memory, "MAX_SOURCE_BYTES", 4 * 1024 * 1024)
    inputs = compile_memory.snapshot_compile_inputs([daily])
    provider = _provider("large")
    monkeypatch.setattr(
        compile_memory, "provider_candidates", lambda *args, **kwargs: [provider]
    )
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    monkeypatch.setattr(compile_memory, "MAX_PROVIDER_RESPONSE_BYTES", 32)
    monkeypatch.setattr(
        compile_memory,
        "call_candidate",
        lambda descriptor, *args, **kwargs: LLMResult(
            descriptor, "x" * 33, True, None, "native"
        ),
    )
    # The lineage is the whole diagnosis: without it this failure says only that
    # nothing worked, which cost hours of guessing on a live vault.
    with pytest.raises(RuntimeError, match=r"validated compile plan: \w+:"):
        compile_memory.resolve_compile_plan(
            inputs,
            CompileCache(state_root),
            coordinator=MarkdownCoordinator(root, state_root),
        )


def test_after_image_cap_fails_before_transaction_prepare(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    monkeypatch.setattr(compile_memory, "MAX_AFTER_IMAGE_BYTES", 32)
    coordinator = MarkdownCoordinator(root, state_root)
    with pytest.raises(ValueError, match="after-image"):
        compile_memory.apply_compile_plan(
            inputs,
            _plan(),
            action_key="9" * 64,
            trigger="manual",
            coordinator=coordinator,
        )
    with coordinator._connect() as database:
        assert database.execute('SELECT * FROM "transaction"').fetchall() == []


def test_resolve_requires_coordinator_and_checks_persisted_gate_before_probe(
    vault, monkeypatch
):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    with pytest.raises(TypeError):
        compile_memory.resolve_compile_plan(inputs, CompileCache(state_root))

    owner = MarkdownCoordinator(root, state_root)
    observer = MarkdownCoordinator(root, state_root)
    monkeypatch.setattr(
        compile_memory,
        "probe_candidate",
        lambda descriptor: pytest.fail("probe ran under persisted writer ownership"),
    )
    with owner.writer_gate():
        with pytest.raises(RuntimeError, match="writer"):
            compile_memory.resolve_compile_plan(
                inputs, CompileCache(state_root), coordinator=observer
            )


def test_critique_failure_lineage_records_stage_provider_and_stable_code(
    vault, monkeypatch
):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    first, second = _provider("first", 0), _provider("second", 1)
    monkeypatch.setattr(
        compile_memory, "provider_candidates", lambda *args, **kwargs: [first, second]
    )
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    draft = json.dumps({"operations": [_semantic()], "audit": {}})
    responses = iter(
        [
            (draft, None),
            (None, "provider_error"),
            (draft, None),
            ('{"reviews": []}', None),
        ]
    )

    def call(descriptor, *args, **kwargs):
        text, failure = next(responses)
        return LLMResult(descriptor, text, True, failure, "native")

    monkeypatch.setattr(compile_memory, "call_candidate", call)
    resolved = compile_memory.resolve_compile_plan(
        inputs,
        CompileCache(state_root),
        coordinator=MarkdownCoordinator(root, state_root),
    )

    assert resolved.action.draft_calls[0].fallback_from == (
        "critique:first:first-model:provider_error",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", 7),
        ("title", "bad\ntitle"),
        ("related", "not-an-array"),
        ("slug", "UPPER"),
        ("category", "unknown"),
    ],
)
def test_normalization_rejects_wrong_types_and_bounds(vault, field, value):
    root, _state_root = vault
    daily = _daily(root)
    semantic = _semantic()
    semantic[field] = value
    plan = _plan()
    plan["operations"][0]["content"] = canonical_json_bytes(semantic).decode()
    import compile_memory

    with pytest.raises(ValueError):
        compile_memory.validate_compile_plan(
            plan, compile_memory.snapshot_compile_inputs([daily])
        )


def test_evidence_requires_exact_date_and_unique_timestamp_block(vault):
    root, _state_root = vault
    daily = root / "knowledge/daily/2026-07-14.md"
    daily.write_text(
        "## [10:00:00] one\ndurable fact\n## [10:00:00] two\ndurable fact\n",
        encoding="utf-8",
    )
    import compile_memory

    with pytest.raises(ValueError, match="ambiguous"):
        compile_memory.validate_compile_plan(
            _plan(), compile_memory.snapshot_compile_inputs([daily])
        )


def test_receipt_evidence_is_source_scoped_and_operation_associated(vault):
    root, state_root = vault
    daily = _daily(root)
    coordinator, inputs, _result = _compile(root, state_root, daily)
    import compile_memory

    source = inputs.dailies[0]
    record = compile_memory.read_compile_receipt_v3(
        source.logical_path, source.sha256, coordinator
    )
    assert record is not None
    assert "completed_at" not in record
    assert record["evidence"] == [
        {
            "source_identity": compile_memory.compile_source_identity(
                source.logical_path, source.sha256
            ),
            "operation_path": "knowledge/notes/safe-note.md",
            "quote_sha256": sha256_bytes(b"durable fact"),
            "source_digest": source.sha256,
            "source_path": "knowledge/daily/2026-07-14.md",
        }
    ]


def test_index_rejects_invalid_utf8_instead_of_replacement_decoding(vault):
    root, _state_root = vault
    (root / "knowledge/notes/bad.md").write_bytes(b"\xff")
    import rebuild_memory_index

    with pytest.raises(UnicodeDecodeError):
        rebuild_memory_index.build_index_bytes(root)


def test_diagnostic_state_cas_keeps_newer_commit_last_fields():
    import compile_memory

    state: dict[str, object] = {}
    compile_memory.merge_compile_diagnostics(
        state,
        commit_sequence=9,
        committed_at="2026-07-14T12:00:09Z",
        hashes={"new.md": "b" * 64},
        operation_id="compile:new",
        action_key="b" * 64,
        touched=("new",),
        trigger="auto",
    )
    compile_memory.merge_compile_diagnostics(
        state,
        commit_sequence=8,
        committed_at="2026-07-14T12:00:08Z",
        hashes={"old.md": "a" * 64},
        operation_id="compile:old",
        action_key="a" * 64,
        touched=("old",),
        trigger="manual",
    )

    assert state["compiled_daily_hashes"] == {
        "new.md": "b" * 64,
        "old.md": "a" * 64,
    }
    assert state["last_compile_operation_id"] == "compile:new"
    assert state["last_compile_commit_sequence"] == 9


def test_diagnostic_state_cas_does_not_regress_same_daily_hash():
    import compile_memory

    state: dict[str, object] = {}
    common = {
        "touched": (),
        "trigger": "auto",
        "action_key": "a" * 64,
    }
    compile_memory.merge_compile_diagnostics(
        state,
        commit_sequence=11,
        committed_at="2026-07-14T12:00:11Z",
        hashes={"daily.md": "b" * 64},
        operation_id="compile:new",
        **common,
    )
    compile_memory.merge_compile_diagnostics(
        state,
        commit_sequence=10,
        committed_at="2026-07-14T12:00:10Z",
        hashes={"daily.md": "a" * 64},
        operation_id="compile:old",
        **common,
    )

    assert state["compiled_daily_hashes"]["daily.md"] == "b" * 64


def test_legacy_direct_mutation_entry_points_are_removed():
    import compile_memory

    assert not hasattr(compile_memory, "run_compile")
    assert not hasattr(compile_memory, "_execute_plan")
    assert not hasattr(compile_memory, "_critique_plan")
