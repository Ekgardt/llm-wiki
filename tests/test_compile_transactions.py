from __future__ import annotations

import json
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from compile_cache import CompileCache
from llm_client import LLMResult, ProviderDescriptor
from markdown_transaction import MarkdownCoordinator
from reliable_memory import canonical_json_bytes, sha256_bytes, validate_schema


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Old index\n")
    (root / "knowledge/log.md").write_bytes(b"# Session Memory Log\n")
    (root / "AGENTS.md").write_bytes(b"agent contract\r\n")

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


def _daily(root: Path) -> Path:
    path = root / "knowledge/daily/2026-07-14.md"
    path.write_bytes(
        b"## [10:00:00] session-end | manual\r\n"
        b"A durable exact-byte observation.\r\n"
    )
    return path


def _semantic_plan() -> dict[str, object]:
    operation = {
        "action": "create",
        "category": "patterns",
        "slug": "exact-byte-pattern",
        "title": "Exact Byte Pattern",
        "summary": "Compile the bytes that were actually reviewed.",
        "body_section": "Lesson",
        "body_markdown": "Use an immutable snapshot so later appends remain pending.",
        "evidence": [
            {
                "daily_date": "2026-07-14",
                "timestamp": "10:00:00",
                "quoted_text": "A durable exact-byte observation.",
                "claim": "The reviewed source is immutable.",
            }
        ],
        "related": [],
    }
    return {
        "schema_version": "compile-plan/v2",
        "operations": [
            {
                "kind": "create",
                "path": "knowledge/notes/exact-byte-pattern.md",
                "content": canonical_json_bytes(operation).decode("utf-8"),
            }
        ],
    }


def _draft_response() -> str:
    semantic = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    return json.dumps({"operations": [semantic], "audit": {}})


def _provider(name: str = "fake", index: int = 0) -> ProviderDescriptor:
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


def test_snapshot_compile_inputs_preserves_exact_bytes(vault):
    root, _state_root = vault
    daily = _daily(root)

    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    original = daily.read_bytes()
    daily.write_bytes(original + b"later\n")

    assert inputs.dailies[0].content == original
    assert inputs.dailies[0].sha256 == sha256_bytes(original)
    assert [source.logical_path for source in inputs.sources] == sorted(
        source.logical_path for source in inputs.sources
    )
    assert any(source.content == b"agent contract\r\n" for source in inputs.sources)


def test_index_can_be_built_from_in_memory_note_bytes(vault):
    root, _state_root = vault
    import rebuild_memory_index

    page = (
        b"---\ntype: pattern\n---\n\n# Pending\n\n"
        b"One-sentence summary: visible before publication.\n"
    )
    output = rebuild_memory_index.build_index_bytes(
        root,
        {"knowledge/notes/pending.md": page},
    )

    assert b"[[knowledge/notes/pending]]" in output
    assert b"visible before publication" in output
    assert not (root / "knowledge/notes/pending.md").exists()


def test_compile_transaction_commits_page_index_log_and_receipt(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    result = compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="a" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    receipt = root / f"knowledge/daily/receipts/{inputs.dailies[0].sha256}.md"
    assert result.state == "committed"
    assert (root / "knowledge/notes/exact-byte-pattern.md").is_file()
    assert b"exact-byte-pattern" in (root / "knowledge/index.md").read_bytes()
    assert inputs.dailies[0].sha256.encode() in (root / "knowledge/log.md").read_bytes()
    assert receipt.is_file()
    text = receipt.read_text(encoding="utf-8")
    assert text.startswith("---\ntype: compile-receipt\n")
    record = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert canonical_json_bytes(record).decode() in text
    assert record["source_digest"] == inputs.dailies[0].sha256
    assert record["action_key"] == "a" * 64
    assert record["state"] == "completed"
    assert record["operation_id"] == result.operation_id
    assert record["evidence"][0]["quote_sha256"] == sha256_bytes(
        b"A durable exact-byte observation."
    )
    validate_schema(
        record,
        Path(compile_memory.__file__).with_name("schemas") / "compile-receipt-v2.json",
    )


def test_append_after_snapshot_remains_pending_even_after_receipt(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    daily.write_bytes(daily.read_bytes() + b"later append\n")
    compile_memory.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="b" * 64,
        trigger="auto",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    selected = compile_memory.select_dailies(
        Namespace(file=None, all=False),
        {"compiled_daily_hashes": {daily.name: inputs.dailies[0].sha256}},
        coordinator=MarkdownCoordinator(root, state_root),
    )
    assert selected == [daily]


def test_legacy_hash_without_v2_receipt_forces_compile(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    selected = compile_memory.select_dailies(
        Namespace(file=None, all=False),
        {"compiled_daily_hashes": {daily.name: sha256_bytes(daily.read_bytes())}},
        coordinator=MarkdownCoordinator(root, state_root),
    )

    assert selected == [daily]


def test_resolver_refuses_llm_work_under_writer_gate(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    coordinator = MarkdownCoordinator(root, state_root)
    with coordinator.writer_gate():
        with pytest.raises(RuntimeError, match="forbidden under the writer gate"):
            compile_memory.resolve_compile_plan(
                inputs,
                CompileCache(state_root),
                coordinator=coordinator,
            )


def test_resolver_uses_exact_snapshot_for_draft_and_critique_and_caches(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    calls = []
    provider = _provider()
    responses = [_draft_response(), json.dumps({"reviews": [{"slug": "exact-byte-pattern", "verdict": "pass", "reason": "ok"}]})]

    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)

    def call(descriptor, prompt, system_prompt, **kwargs):
        calls.append((descriptor, prompt, kwargs.get("schema")))
        return LLMResult(descriptor, responses.pop(0), True, None, "native")

    monkeypatch.setattr(compile_memory, "call_candidate", call)
    cache = CompileCache(state_root)
    resolved = compile_memory.resolve_compile_plan(
        inputs, cache, coordinator=MarkdownCoordinator(root, state_root)
    )

    exact = inputs.dailies[0].content.decode("utf-8")
    assert len(calls) == 2
    assert all(exact in prompt for _descriptor, prompt, _schema in calls)
    assert all(schema is not None for _descriptor, _prompt, schema in calls)
    assert resolved.action.draft_calls[0].structured_output == "native"
    assert resolved.action.critique_calls[0].structured_output == "native"
    assert resolved.cache_hit is False
    assert cache.get(
        resolved.action,
        lambda plan: compile_memory.validate_compile_plan(plan, inputs),
    ) == resolved.plan


def test_resolver_cache_hit_revalidates_without_llm(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    provider = _provider()
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    responses = [_draft_response(), '{"reviews": []}']
    monkeypatch.setattr(
        compile_memory,
        "call_candidate",
        lambda descriptor, *args, **kwargs: LLMResult(
            descriptor, responses.pop(0), True, None, "native"
        ),
    )
    cache = CompileCache(state_root)
    coordinator = MarkdownCoordinator(root, state_root)
    first = compile_memory.resolve_compile_plan(inputs, cache, coordinator=coordinator)
    monkeypatch.setattr(
        compile_memory,
        "call_candidate",
        lambda *args, **kwargs: pytest.fail("cache hit called the LLM"),
    )

    second = compile_memory.resolve_compile_plan(inputs, cache, coordinator=coordinator)

    assert second.plan == first.plan
    assert second.action == first.action
    assert second.cache_hit is True


def test_failed_evidence_is_not_cached(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    provider = _provider()
    bad = json.loads(_draft_response())
    bad["operations"][0]["evidence"][0]["quoted_text"] = "not in source"
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    monkeypatch.setattr(
        compile_memory,
        "call_candidate",
        lambda descriptor, *args, **kwargs: LLMResult(
            descriptor, json.dumps(bad), True, None, "native"
        ),
    )

    with pytest.raises(RuntimeError, match="validated compile plan"):
        compile_memory.resolve_compile_plan(
            inputs,
            CompileCache(state_root),
            coordinator=MarkdownCoordinator(root, state_root),
        )

    assert not list((state_root / "cache/compile").glob("*.json"))


def test_provider_failure_recomputes_descriptor_with_actual_fallback(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    first = _provider("first", 0)
    second = _provider("second", 1)
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [first, second])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    responses = iter([None, _draft_response(), '{"reviews": []}'])

    def call(descriptor, *args, **kwargs):
        text = next(responses)
        return LLMResult(
            descriptor,
            text,
            True,
            "provider_error" if text is None else None,
            "native",
        )

    monkeypatch.setattr(compile_memory, "call_candidate", call)
    resolved = compile_memory.resolve_compile_plan(
        inputs,
        CompileCache(state_root),
        coordinator=MarkdownCoordinator(root, state_root),
    )

    assert resolved.action.draft_calls[0].provider == "second"
    assert resolved.action.draft_calls[0].fallback_from == (
        "draft:first:first-model:provider_error",
    )


def test_mid_apply_failure_recovers_complete_compile_tree(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    coordinator = MarkdownCoordinator(root, state_root)
    original = coordinator._apply_operation
    calls = 0

    def crash_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated process interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_apply_operation", crash_after_first)
    with pytest.raises(OSError, match="interruption"):
        compile_memory.apply_compile_plan(
            inputs,
            _semantic_plan(),
            action_key="c" * 64,
            trigger="manual",
            coordinator=coordinator,
            completed_at="2026-07-14T12:00:00Z",
        )

    receipt = root / f"knowledge/daily/receipts/{inputs.dailies[0].sha256}.md"
    assert not receipt.exists()
    monkeypatch.setattr(coordinator, "_apply_operation", original)
    recovered = coordinator.recover()

    assert recovered[-1].state == "committed"
    assert receipt.is_file()
    assert (root / "knowledge/notes/exact-byte-pattern.md").is_file()
    assert b"exact-byte-pattern" in (root / "knowledge/index.md").read_bytes()


def test_concurrent_agents_publish_snapshot_once(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])

    def publish(_index):
        return compile_memory.apply_compile_plan(
            inputs,
            _semantic_plan(),
            action_key="d" * 64,
            trigger="auto",
            coordinator=MarkdownCoordinator(root, state_root),
            completed_at="2026-07-14T12:00:00Z",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, range(2)))

    assert {result.operation_id for result in results} == {results[0].operation_id}
    assert len(list((root / "knowledge/daily/receipts").glob("*.md"))) == 1
    assert (root / "knowledge/log.md").read_text(encoding="utf-8").count(
        "compile completed for snapshot"
    ) == 1
    assert (root / "knowledge/notes/exact-byte-pattern.md").read_text(
        encoding="utf-8"
    ).count("# Exact Byte Pattern") == 1


def test_cache_contains_semantics_not_rendered_markdown(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    provider = _provider()
    responses = [_draft_response(), '{"reviews": []}']
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    monkeypatch.setattr(
        compile_memory,
        "call_candidate",
        lambda descriptor, *args, **kwargs: LLMResult(
            descriptor, responses.pop(0), True, None, "native"
        ),
    )

    resolved = compile_memory.resolve_compile_plan(
        inputs,
        CompileCache(state_root),
        coordinator=MarkdownCoordinator(root, state_root),
    )
    cache_bytes = next((state_root / "cache/compile").glob("*.json")).read_bytes()

    assert b"One-sentence summary:" not in cache_bytes
    assert b"2026-07-14T12:00:00Z" not in cache_bytes
    assert b"knowledge/index.md" not in canonical_json_bytes(resolved.plan)
    assert b"knowledge/log.md" not in canonical_json_bytes(resolved.plan)


def test_run_records_snapshot_hash_only_after_commit(vault, monkeypatch):
    root, _state_root = vault
    daily = _daily(root)
    original = daily.read_bytes()
    import compile_memory

    state: dict[str, object] = {"compiled_daily_hashes": {}}
    monkeypatch.setattr(compile_memory, "load_state", lambda: state)
    monkeypatch.setattr(
        compile_memory,
        "update_state",
        lambda mutate: mutate(state),
    )
    monkeypatch.setattr(compile_memory, "_mark_finished", lambda *args, **kwargs: None)

    def resolve(inputs, cache, *, coordinator):
        daily.write_bytes(original + b"later append\n")
        return SimpleNamespace(
            plan={"schema_version": "compile-plan/v2", "operations": []},
            action_key="e" * 64,
            cache_hit=False,
        )

    monkeypatch.setattr(compile_memory, "resolve_compile_plan", resolve)
    result = compile_memory._run(
        Namespace(file=None, all=False, dry_run=False, trigger="manual")
    )

    assert result == 0
    assert state["compiled_daily_hashes"] == {
        daily.name: sha256_bytes(original),
    }
    assert compile_memory.select_dailies(
        Namespace(file=None, all=False),
        state,
        coordinator=MarkdownCoordinator(root, compile_memory.STATE_ROOT),
    ) == [daily]


def test_evidence_quote_must_be_inside_cited_timestamp_block(vault):
    root, _state_root = vault
    daily = root / "knowledge/daily/2026-07-14.md"
    daily.write_bytes(
        b"## [10:00:00] first\nDifferent claim.\n"
        b"## [11:00:00] second\nA durable exact-byte observation.\n"
    )
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])

    with pytest.raises(ValueError, match="immutable snapshot"):
        compile_memory.validate_compile_plan(_semantic_plan(), inputs)


def test_prompt_fallback_output_is_schema_checked_before_cache(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    provider = _provider()
    malformed = json.loads(_draft_response())
    malformed["operations"] = []
    malformed["unexpected"] = True
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    monkeypatch.setattr(
        compile_memory,
        "call_candidate",
        lambda descriptor, *args, **kwargs: LLMResult(
            descriptor, json.dumps(malformed), True, None, "native"
        ),
    )

    with pytest.raises(RuntimeError, match="validated compile plan"):
        compile_memory.resolve_compile_plan(
            inputs,
            CompileCache(state_root),
            coordinator=MarkdownCoordinator(root, state_root),
        )

    assert not list((state_root / "cache/compile").glob("*.json"))
