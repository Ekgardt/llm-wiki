from __future__ import annotations

import json
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
    for relative in ("knowledge/daily/receipts", "knowledge/notes", "knowledge/projects"):
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
        b"The prior state is blue.\r\n"
        b"A second durable exact-byte observation.\r\n"
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


def _claim_record(root: Path, *, claim_id: str, value: str, text: str, authority: str) -> dict[str, object]:
    daily = (root / "knowledge/daily/2026-07-14.md").read_bytes()
    start = daily.index(text.encode())
    semantic = {
        "subject": "project",
        "relation": "has-state",
        "value": {"type": "string", "value": value},
        "qualifiers": [],
        "validity": {"from": "2026-07-14", "to": None},
    }
    return {
        "schema_version": "claim/v1",
        "id": claim_id,
        "fingerprint": sha256_bytes(canonical_json_bytes(semantic)),
        "text": text,
        **semantic,
        "observed_at": "2026-07-14T10:00:00Z",
        "lifecycle": "active",
        "confidence": "high",
        "authority": authority,
        "evidence": {
            "reference": (
                f"daily:2026-07-14 sha256:{sha256_bytes(daily)} "
                f"block:10:00:00 bytes:{start}-{start + len(text.encode())}"
            ),
            "sha256": sha256_bytes(text.encode()),
            "text": text,
        },
        "links": [],
        "extractor_version": "test/v1",
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


def test_source_identity_hashes_logical_path_and_digest(vault):
    root, _state_root = vault
    import compile_memory

    shared = b"same bytes\n"
    first = root / "knowledge/daily/2026-07-14.md"
    second = root / "knowledge/daily/2026-07-15.md"
    first.write_bytes(shared)
    second.write_bytes(shared)
    digest = sha256_bytes(shared)

    first_identity = compile_memory.compile_source_identity(
        "knowledge/daily/2026-07-14.md", digest
    )
    second_identity = compile_memory.compile_source_identity(
        "knowledge/daily/2026-07-15.md", digest
    )

    assert first_identity == sha256_bytes(
        canonical_json_bytes(["knowledge/daily/2026-07-14.md", digest])
    )
    assert second_identity == sha256_bytes(
        canonical_json_bytes(["knowledge/daily/2026-07-15.md", digest])
    )
    assert first_identity != second_identity
    assert compile_memory.compile_receipt_path(first_identity).name == (
        f"v3-{first_identity}.md"
    )
    assert compile_memory.compile_receipt_path(second_identity).name == (
        f"v3-{second_identity}.md"
    )


def test_complete_item_packer_rejects_oversized_daily_before_dispatch(vault):
    _root, _state_root = vault
    import compile_memory

    content = b"x" * 28_000
    digest = sha256_bytes(content)
    daily = compile_memory.DailySnapshot(
        "knowledge/daily/2026-07-14.md", content, digest
    )
    inputs = compile_memory.CompileInputs(
        dailies=(daily,),
        sources=(
            compile_memory.SourceSnapshot(daily.logical_path, content, digest),
        ),
        targets=(),
    )

    with pytest.raises(ValueError, match="daily source exceeds compile input budget"):
        compile_memory.pack_compile_batches(inputs, model=None)


def test_complete_item_packer_drops_oversized_optional_context(vault):
    _root, _state_root = vault
    import compile_memory

    daily_content = b"## [10:00:00] event\ndurable fact\n"
    daily = compile_memory.DailySnapshot(
        "knowledge/daily/2026-07-14.md",
        daily_content,
        sha256_bytes(daily_content),
    )
    context_content = b"x" * 28_000
    context = compile_memory.SourceSnapshot(
        "knowledge/notes/optional.md",
        context_content,
        sha256_bytes(context_content),
    )
    inputs = compile_memory.CompileInputs(
        dailies=(daily,),
        sources=(
            compile_memory.SourceSnapshot(
                daily.logical_path, daily.content, daily.sha256
            ),
            context,
        ),
        targets=(
            compile_memory.TargetSnapshot(
                context.logical_path, context.content, context.sha256
            ),
        ),
    )

    batches = compile_memory.pack_compile_batches(inputs, model=None)

    assert len(batches) == 1
    assert [item.logical_path for item in batches[0].inputs.sources] == [
        daily.logical_path
    ]
    assert batches[0].inputs.targets == inputs.targets


def test_critique_receives_cited_evidence_without_uncited_source_blob(vault):
    root, _state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))

    prompt = compile_memory._critique_prompt(inputs, [operation])

    assert "A durable exact-byte observation." in prompt
    assert "A second durable exact-byte observation." not in prompt
    assert "The prior state is blue." not in prompt
    assert "knowledge/daily/2026-07-14.md" in prompt
    assert inputs.dailies[0].sha256 in prompt


def test_critique_budget_fails_before_second_provider_call(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    provider = _provider()
    calls = []

    monkeypatch.setattr(
        compile_memory, "provider_candidates", lambda *args, **kwargs: [provider]
    )
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)

    def call(descriptor, prompt, system_prompt, **kwargs):
        calls.append(prompt)
        return LLMResult(descriptor, _draft_response(), True, None, "native")

    def token_counter(text):
        return 30_000 if "CITED EVIDENCE" in text else 100

    monkeypatch.setattr(compile_memory, "call_candidate", call)

    with pytest.raises(RuntimeError, match="validated compile plan"):
        compile_memory.resolve_compile_plan(
            batch.inputs,
            CompileCache(state_root),
            coordinator=MarkdownCoordinator(root, state_root),
            batch=batch,
            token_adapters={provider.model: token_counter},
        )

    assert len(calls) == 1


def test_v3_receipt_path_and_body_bind_source_path_not_only_digest(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model="fake-v1")[0]
    result = compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="a" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
        batch=batch,
        provider_budget={
            "provider": "fake",
            "model": "fake-v1",
            "max_output_tokens": 4000,
        },
    )
    source = batch.manifest[0]
    source_identity = compile_memory.compile_source_identity(
        source.logical_path, source.sha256
    )
    receipt = root / f"knowledge/daily/receipts/v3-{source_identity}.md"

    assert receipt.is_file()
    assert not (root / f"knowledge/daily/receipts/{source.sha256}.md").exists()
    record = compile_memory.parse_compile_receipt_v3(
        receipt.read_bytes(),
        logical_path=source.logical_path,
        source_sha256=source.sha256,
    )
    assert record["source_identity"] == source_identity
    assert record["source"] == source.receipt_descriptor()
    assert record["batch_manifest_sha256"] == batch.manifest_sha256
    assert record["operation_id"] == result.operation_id
    assert "completed_at" not in record


def test_oversized_prospective_receipt_fails_before_writer_gate(
    vault, monkeypatch
):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    coordinator = MarkdownCoordinator(root, state_root)
    entered = False

    @contextmanager
    def observed_gate(*args, **kwargs):
        nonlocal entered
        entered = True
        pytest.fail("writer gate opened before receipt preflight")
        yield

    monkeypatch.setattr(coordinator, "writer_gate", observed_gate)
    monkeypatch.setattr(compile_memory, "MAX_RECEIPT_BYTES", 512)

    with pytest.raises(ValueError, match="receipt exceeds"):
        compile_memory.apply_compile_plan(
            inputs,
            _semantic_plan(),
            action_key="b" * 64,
            trigger="manual",
            coordinator=coordinator,
            batch=batch,
            provider_budget={
                "provider": "fake",
                "model": "fake-v1",
                "max_output_tokens": 4000,
            },
        )

    assert entered is False
    assert not list((root / "knowledge/daily/receipts").glob("*.md"))


def test_v3_compile_uses_supplied_canonical_owner(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory
    import markdown_transaction
    import operational_ownership

    candidate = state_root / "run/markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(
        candidate, source_v2=None
    )
    owner, marker = operational_ownership.acquire_compile_owner(
        state_root=state_root
    )
    coordinator = MarkdownCoordinator._from_v3_candidate(
        candidate, state_root=state_root
    )
    coordinator.vault = root.resolve()
    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    try:
        result = compile_memory.apply_compile_plan(
            inputs,
            _semantic_plan(),
            action_key="c" * 64,
            trigger="manual",
            coordinator=coordinator,
            batch=batch,
            provider_budget={
                "provider": "fake",
                "model": "fake-v1",
                "max_output_tokens": 4000,
            },
            owner=owner,
        )
    finally:
        operational_ownership.release_marker_owner(owner, marker)

    assert result.state == "committed"


def test_successful_v3_retry_keeps_operation_receipt_path_and_bytes(
    vault, monkeypatch
):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    coordinator = MarkdownCoordinator(root, state_root)
    real_apply = coordinator.apply

    def commit_then_fail(*args, **kwargs):
        real_apply(*args, **kwargs)
        raise OSError("commit return was lost")

    monkeypatch.setattr(coordinator, "apply", commit_then_fail)
    with pytest.raises(OSError, match="return was lost"):
        compile_memory.apply_compile_plan(
            inputs,
            _semantic_plan(),
            action_key="d" * 64,
            trigger="manual",
            coordinator=coordinator,
            batch=batch,
            provider_budget={
                "provider": "fake",
                "model": "fake-v1",
                "max_output_tokens": 4000,
            },
            completed_at="2026-07-14T12:00:00Z",
        )
    source = batch.manifest[0]
    identity = compile_memory.compile_source_identity(
        source.logical_path, source.sha256
    )
    receipt = root / f"knowledge/daily/receipts/v3-{identity}.md"
    first_bytes = receipt.read_bytes()
    first_record = compile_memory.parse_compile_receipt_v3(
        first_bytes,
        logical_path=source.logical_path,
        source_sha256=source.sha256,
    )

    monkeypatch.setattr(coordinator, "apply", real_apply)
    retried = compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="d" * 64,
        trigger="manual",
        coordinator=coordinator,
        batch=batch,
        provider_budget={
            "provider": "fake",
            "model": "fake-v1",
            "max_output_tokens": 4000,
        },
        completed_at="2026-08-01T00:00:00Z",
    )

    assert retried.operation_id == first_record["operation_id"]
    assert receipt.read_bytes() == first_bytes


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


def test_compile_page_preserves_five_agent_evidence_attribution(vault, monkeypatch):
    root, state_root = vault
    daily = root / "knowledge/daily/2026-07-14.md"
    agents = ("opencode", "codex", "claude", "cursor", "antigravity")
    lines = []
    evidence = []
    for index, agent in enumerate(agents):
        timestamp = f"10:0{index}:00"
        observation = f"Durable observation from {agent}."
        lines.extend(
            [
                f"## [{timestamp}] {agent}-session | session-{index}",
                observation,
            ]
        )
        evidence.append(
            {
                "daily_date": "2026-07-14",
                "timestamp": timestamp,
                "quoted_text": observation,
                "claim": f"{agent} supplied evidence.",
            }
        )
    daily.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import compile_memory

    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["evidence"] = evidence
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [
            {
                "kind": "create",
                "path": "knowledge/notes/exact-byte-pattern.md",
                "content": canonical_json_bytes(operation).decode("utf-8"),
            }
        ],
    }
    inputs = compile_memory.snapshot_compile_inputs([daily])

    compile_memory.apply_compile_plan(
        inputs,
        plan,
        action_key="b" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-08-16T12:00:00Z",
    )

    import agent_timeline

    monkeypatch.setattr(agent_timeline, "ROOT", root)
    monkeypatch.setattr(agent_timeline, "KNOWLEDGE", root / "knowledge/notes")
    activity = agent_timeline._extract_knowledge_timeline(None, days=36_500)
    assert {item["agent"] for item in activity} == set(agents)


def test_quarantined_compile_publishes_only_idempotent_candidates_and_stays_pending(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory
    from claims import ClaimIndex, NormalizedClaim

    old = _claim_record(
        root,
        claim_id="old",
        value="blue",
        text="The prior state is blue.",
        authority="user",
    )
    existing = root / "knowledge/notes/existing.md"
    existing.write_bytes(
        b"---\ntype: concept\n---\n# Existing\n\n## Claims\n```json\n"
        + canonical_json_bytes({"schema_version": "claim-ledger/v1", "claims": [old]})
        + b"\n```\n"
    )
    new = _claim_record(
        root,
        claim_id="new",
        value="red",
        text="A durable exact-byte observation.",
        authority="inferred",
    )
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["claims"] = [new]
    inputs = compile_memory.snapshot_compile_inputs([daily])
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [{
            "kind": "create",
            "path": "knowledge/notes/exact-byte-pattern.md",
            "content": canonical_json_bytes(operation).decode(),
        }],
    }
    index = ClaimIndex(state_root, vault=root)
    index.rebuild()
    coordinator = MarkdownCoordinator(root, state_root)

    result = compile_memory.apply_compile_plan(
        inputs,
        plan,
        action_key="9" * 64,
        trigger="manual",
        coordinator=coordinator,
        completed_at="2026-07-14T12:00:00Z",
    )

    page = root / "knowledge/notes/exact-byte-pattern.md"
    receipt = root / f"knowledge/daily/receipts/{inputs.dailies[0].sha256}.md"
    assert not page.exists()
    assert not receipt.exists()
    assert b"exact-byte-pattern" not in (root / "knowledge/index.md").read_bytes()
    assert b"Use an immutable snapshot" not in (root / "knowledge/log.md").read_bytes()
    import search_memory

    original_knowledge = search_memory.KNOWLEDGE_DIR
    search_memory.KNOWLEDGE_DIR = root / "knowledge/notes"
    try:
        assert page not in search_memory._collect_pages()
    finally:
        search_memory.KNOWLEDGE_DIR = original_knowledge
    candidates = list((root / "knowledge/inbox/claims").glob("*.md"))
    assert len(candidates) == 1
    transaction = coordinator._record_for_operation_id(result.operation_id)
    assert transaction is not None
    assert result.operation_id.startswith("compile-quarantine:")
    assert {item.path for item in transaction.operations} == {
        candidates[0].relative_to(root).as_posix()
    }
    assert all(
        item.page != "knowledge/notes/exact-byte-pattern.md"
        for item in index.candidates(NormalizedClaim(new))
    )
    selected = compile_memory.select_dailies(
        Namespace(file=None, all=False), {}, coordinator=coordinator
    )
    assert selected == [daily]

    retried = compile_memory.apply_compile_plan(
        inputs,
        plan,
        action_key="9" * 64,
        trigger="manual",
        coordinator=coordinator,
        completed_at="2026-07-14T12:00:00Z",
    )
    assert retried.transaction_id == result.transaction_id
    assert len(list((root / "knowledge/inbox/claims").glob("*.md"))) == 1


def test_compile_batch_claims_compare_incrementally_and_mutual_conflict_quarantines(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    first = _claim_record(
        root, claim_id="first", value="red",
        text="A durable exact-byte observation.", authority="user",
    )
    second = _claim_record(
        root, claim_id="second", value="green",
        text="A second durable exact-byte observation.", authority="user",
    )
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["claims"] = [first, second]
    inputs = compile_memory.snapshot_compile_inputs([daily])
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [{
            "kind": "create", "path": "knowledge/notes/exact-byte-pattern.md",
            "content": canonical_json_bytes(operation).decode(),
        }],
    }

    compile_memory.apply_compile_plan(
        inputs, plan, action_key="8" * 64, trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    assert not (root / "knowledge/notes/exact-byte-pattern.md").exists()
    assert len(list((root / "knowledge/inbox/claims").glob("*.md"))) == 2


def test_duplicate_claim_ids_are_rejected_before_any_transaction(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    duplicate = _claim_record(
        root, claim_id="duplicate", value="red",
        text="A durable exact-byte observation.", authority="user",
    )
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["claims"] = [duplicate, duplicate]
    inputs = compile_memory.snapshot_compile_inputs([daily])
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [{
            "kind": "create", "path": "knowledge/notes/exact-byte-pattern.md",
            "content": canonical_json_bytes(operation).decode(),
        }],
    }
    coordinator = MarkdownCoordinator(root, state_root)

    with pytest.raises(ValueError, match="duplicate claim id"):
        compile_memory.apply_compile_plan(
            inputs, plan, action_key="7" * 64, trigger="manual",
            coordinator=coordinator, completed_at="2026-07-14T12:00:00Z",
        )

    assert not (root / "knowledge/notes/exact-byte-pattern.md").exists()
    with coordinator._connect() as database:
        assert database.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0] == 0


def test_postcommit_claim_index_rebuild_failure_invalidates_without_failing_commit(
    vault, monkeypatch
):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory
    from claims import ClaimIndex

    new = _claim_record(
        root, claim_id="new", value="red",
        text="A durable exact-byte observation.", authority="user",
    )
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["claims"] = [new]
    inputs = compile_memory.snapshot_compile_inputs([daily])
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [{
            "kind": "create", "path": "knowledge/notes/exact-byte-pattern.md",
            "content": canonical_json_bytes(operation).decode(),
        }],
    }
    original_rebuild = ClaimIndex.rebuild
    calls = 0

    def fail_after_commit(self, sources=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_rebuild(self, sources)
        raise OSError("derived cache failure")

    monkeypatch.setattr(ClaimIndex, "rebuild", fail_after_commit)
    monkeypatch.setattr(compile_memory, "default_secondary_search", lambda *args: [])

    result = compile_memory.apply_compile_plan(
        inputs, plan, action_key="6" * 64, trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    assert result.state == "committed"
    assert (root / "knowledge/notes/exact-byte-pattern.md").is_file()
    assert not (state_root / "cache/claims.sqlite3").exists()


def test_new_claim_page_inserted_after_assessment_fails_tree_manifest_precondition(
    vault, monkeypatch
):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory
    from markdown_transaction import TransactionFailure

    new = _claim_record(
        root, claim_id="new", value="red",
        text="A durable exact-byte observation.", authority="user",
    )
    phantom = _claim_record(
        root, claim_id="phantom", value="green",
        text="A second durable exact-byte observation.", authority="user",
    )
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["claims"] = [new]
    inputs = compile_memory.snapshot_compile_inputs([daily])
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [{
            "kind": "create", "path": "knowledge/notes/exact-byte-pattern.md",
            "content": canonical_json_bytes(operation).decode(),
        }],
    }
    coordinator = MarkdownCoordinator(root, state_root)
    original_apply = coordinator.apply

    def insert_phantom_then_apply(transaction_id, **kwargs):
        (root / "knowledge/notes/phantom.md").write_bytes(
            b"---\ntype: concept\n---\n# Phantom\n\n## Claims\n```json\n"
            + canonical_json_bytes(
                {"schema_version": "claim-ledger/v1", "claims": [phantom]}
            )
            + b"\n```\n"
        )
        return original_apply(transaction_id, **kwargs)

    monkeypatch.setattr(coordinator, "apply", insert_phantom_then_apply)
    monkeypatch.setattr(compile_memory, "default_secondary_search", lambda *args: [])

    with pytest.raises(TransactionFailure, match="claim tree manifest"):
        compile_memory.apply_compile_plan(
            inputs, plan, action_key="5" * 64, trigger="manual",
            coordinator=coordinator, completed_at="2026-07-14T12:00:00Z",
        )

    assert not (root / "knowledge/notes/exact-byte-pattern.md").exists()
    assert not list((root / "knowledge/daily/receipts").glob("*.md"))
    with coordinator._connect() as database:
        row = database.execute(
            'SELECT preconditions_json FROM "transaction" ORDER BY rowid DESC LIMIT 1'
        ).fetchone()
    assert row is not None
    assert "claim_tree_manifest" in json.loads(row["preconditions_json"])


def test_compile_same_id_replacement_after_assessment_quarantines_without_mutation(
    vault, monkeypatch
):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory
    import contradiction_pipeline

    old = _claim_record(
        root, claim_id="shared", value="blue",
        text="The prior state is blue.", authority="web",
    )
    replacement = {**old, "value": {"type": "string", "value": "red"}}
    replacement["fingerprint"] = sha256_bytes(
        canonical_json_bytes(
            {
                "subject": replacement["subject"],
                "relation": replacement["relation"],
                "value": replacement["value"],
                "qualifiers": replacement["qualifiers"],
                "validity": replacement["validity"],
            }
        )
    )
    existing = root / "knowledge/notes/existing.md"

    def write_existing(record):
        existing.write_bytes(
            b"---\ntype: concept\n---\n# Existing\n\n## Claims\n```json\n"
            + canonical_json_bytes(
                {"schema_version": "claim-ledger/v1", "claims": [record]}
            )
            + b"\n```\n"
        )

    write_existing(old)
    new = _claim_record(
        root, claim_id="new", value="green",
        text="A durable exact-byte observation.", authority="user",
    )
    operation = json.loads(str(_semantic_plan()["operations"][0]["content"]))
    operation["claims"] = [new]
    inputs = compile_memory.snapshot_compile_inputs([daily])
    plan = {
        "schema_version": "compile-plan/v2",
        "operations": [{
            "kind": "create",
            "path": "knowledge/notes/exact-byte-pattern.md",
            "content": canonical_json_bytes(operation).decode(),
        }],
    }
    original_assess = contradiction_pipeline.ContradictionPipeline.assess
    replaced = False

    def assess_then_replace(self, *args, **kwargs):
        nonlocal replaced
        result = original_assess(self, *args, **kwargs)
        if result.lifecycle_mutations and not replaced:
            write_existing(replacement)
            replaced = True
        return result

    monkeypatch.setattr(
        contradiction_pipeline.ContradictionPipeline, "assess", assess_then_replace
    )
    monkeypatch.setattr(compile_memory, "default_secondary_search", lambda *args: [])
    coordinator = MarkdownCoordinator(root, state_root)

    result = compile_memory.apply_compile_plan(
        inputs, plan, action_key="4" * 64, trigger="manual",
        coordinator=coordinator, completed_at="2026-07-14T12:00:00Z",
    )

    assert result.operation_id.startswith("compile-quarantine:")
    assert not (root / "knowledge/notes/exact-byte-pattern.md").exists()
    assert len(list((root / "knowledge/inbox/claims").glob("*.md"))) == 1
    current = existing.read_bytes()
    assert replacement["fingerprint"].encode() in current
    assert b'"lifecycle":"active"' in current
    assert b'"lifecycle":"superseded"' not in current


def test_committed_compile_clears_durable_source_failure(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory
    from memory_queue import MemoryQueue

    inputs = compile_memory.snapshot_compile_inputs([daily])
    queue = MemoryQueue(state_root)
    queue.record_source_failure(
        inputs.dailies[0].logical_path,
        inputs.dailies[0].sha256,
        error_code="previous_failure",
        producer="compile",
    )

    compile_memory.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="f" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    assert queue.source_failure(
        inputs.dailies[0].logical_path, inputs.dailies[0].sha256
    ) is None


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


def test_exact_legacy_diagnostic_suppresses_migration_only_compile(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    selected = compile_memory.select_dailies(
        Namespace(file=None, all=False),
        {"compiled_daily_hashes": {daily.name: sha256_bytes(daily.read_bytes())}},
        coordinator=MarkdownCoordinator(root, state_root),
    )

    assert selected == []


def test_v2_receipt_alone_never_suppresses_normal_selection(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    compile_memory.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="7" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    selected = compile_memory.select_dailies(
        Namespace(file=None, all=False),
        {},
        coordinator=MarkdownCoordinator(root, state_root),
    )

    assert selected == [daily]
    assert compile_memory.read_compile_receipt_v2(
        inputs.dailies[0].sha256,
        MarkdownCoordinator(root, state_root),
    ) is not None


@pytest.mark.parametrize(
    "legacy_key",
    [
        "knowledge/daily/2026-07-14.md",
        "../2026-07-14.md",
        "subdir/2026-07-14.md",
        r"subdir\2026-07-14.md",
        ".",
        "",
    ],
)
def test_legacy_diagnostic_key_must_be_exact_flat_daily_basename(
    vault, legacy_key
):
    root, state_root = vault
    daily = _daily(root)
    digest = sha256_bytes(daily.read_bytes())
    import compile_memory

    selected = compile_memory.select_dailies(
        Namespace(file=None, all=False),
        {"compiled_daily_hashes": {legacy_key: digest}},
        coordinator=MarkdownCoordinator(root, state_root),
    )

    assert selected == [daily]


def test_explicit_file_skips_only_exact_v3_authority(vault):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    coordinator = MarkdownCoordinator(root, state_root)
    args = Namespace(file=str(daily), all=False)
    assert compile_memory.select_dailies(args, {}, coordinator=coordinator) == [daily]

    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    compile_memory.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="8" * 64,
        trigger="manual",
        coordinator=coordinator,
        batch=batch,
        provider_budget={
            "provider": "fake",
            "model": "fake-v1",
            "max_output_tokens": 4000,
        },
    )

    assert compile_memory.select_dailies(args, {}, coordinator=coordinator) == []


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


def test_resolver_uses_exact_snapshot_for_draft_and_cited_critique_and_caches(
    vault, monkeypatch
):
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
    assert exact in calls[0][1]
    assert exact not in calls[1][1]
    assert "A durable exact-byte observation." in calls[1][1]
    assert "A second durable exact-byte observation." not in calls[1][1]
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


def test_cancelled_compile_does_not_publish_markdown_after_prepare(vault, monkeypatch):
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    coordinator = MarkdownCoordinator(root, state_root)
    original_prepare = coordinator.prepare
    cancelled = False

    def prepare_then_cancel(*args, **kwargs):
        nonlocal cancelled
        transaction = original_prepare(*args, **kwargs)
        cancelled = True
        return transaction

    monkeypatch.setattr(coordinator, "prepare", prepare_then_cancel)

    with pytest.raises(TimeoutError, match="deadline|cancellation"):
        compile_memory.apply_compile_plan(
            inputs,
            _semantic_plan(),
            action_key="f" * 64,
            trigger="manual",
            coordinator=coordinator,
            completed_at="2026-07-14T12:00:00Z",
            deadline=float("inf"),
            cancelled=lambda: cancelled,
        )

    assert not (root / "knowledge/notes/exact-byte-pattern.md").exists()
    assert (root / "knowledge/index.md").read_bytes() == b"# Old index\n"
    assert (root / "knowledge/log.md").read_bytes() == b"# Session Memory Log\n"
    assert not list((root / "knowledge/daily/receipts").glob("*.md"))


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

    captured = {}

    def resolve(inputs, cache, *, coordinator, batch, token_adapters=None):
        daily.write_bytes(original + b"later append\n")
        captured["batch"] = batch
        return SimpleNamespace(
            plan={"schema_version": "compile-plan/v2", "operations": []},
            action_key="e" * 64,
            cache_hit=False,
            provider_budget={
                "provider": "fake",
                "model": "fake-v1",
                "max_output_tokens": 4000,
            },
        )

    monkeypatch.setattr(compile_memory, "resolve_compile_plan", resolve)
    result = compile_memory._run(
        Namespace(file=None, all=False, dry_run=False, trigger="manual")
    )

    assert result == 0
    assert captured["batch"].inputs.dailies[0].content == original
    assert state["compiled_daily_hashes"] == {
        daily.name: sha256_bytes(original),
    }
    assert compile_memory.select_dailies(
        Namespace(file=None, all=False),
        state,
        coordinator=MarkdownCoordinator(root, compile_memory.STATE_ROOT),
    ) == [daily]


def test_run_does_not_record_provider_failure_after_cancellation(vault, monkeypatch):
    root, _state_root = vault
    _daily(root)
    import compile_memory

    cancelled = False
    recorded = []
    finished = []

    def fail_after_cancellation(*args, **kwargs):
        nonlocal cancelled
        cancelled = True
        raise RuntimeError("provider failed after timeout")

    monkeypatch.setattr(compile_memory, "load_state", lambda: {})
    monkeypatch.setattr(compile_memory, "resolve_compile_plan", fail_after_cancellation)
    monkeypatch.setattr(
        compile_memory,
        "_record_compile_source_failures",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    monkeypatch.setattr(
        compile_memory,
        "_mark_finished",
        lambda *args, **kwargs: finished.append((args, kwargs)),
    )

    with pytest.raises(TimeoutError, match="deadline|cancel"):
        compile_memory._run(
            Namespace(file=None, all=False, dry_run=False, trigger="manual"),
            cancelled=lambda: cancelled,
        )

    assert recorded == []
    assert finished == []


def test_run_packs_before_provider_dispatch(vault, monkeypatch):
    root, _state_root = vault
    daily = _daily(root)
    daily.write_bytes(b"x" * 28_000)
    import compile_memory

    monkeypatch.setattr(compile_memory, "load_state", lambda: {})
    monkeypatch.setattr(compile_memory, "_mark_finished", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        compile_memory,
        "_record_compile_source_failures",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        compile_memory,
        "resolve_compile_plan",
        lambda *args, **kwargs: pytest.fail("provider dispatch ran before packing"),
    )
    monkeypatch.setattr(
        compile_memory,
        "update_state",
        lambda *args, **kwargs: pytest.fail("compile diagnostics were updated"),
    )

    result = compile_memory._run(
        Namespace(file=None, all=False, dry_run=False, trigger="manual")
    )

    assert result == 1
    assert not list((root / "knowledge/daily/receipts").glob("*.md"))


def test_run_refreshes_context_between_compile_batches(vault, monkeypatch):
    root, _state_root = vault
    first = root / "knowledge/daily/2026-07-14.md"
    second = root / "knowledge/daily/2026-07-15.md"
    first.write_bytes(b"a" * 14_000)
    second.write_bytes(b"b" * 14_000)
    import compile_memory

    state: dict[str, object] = {}
    seen_index_hashes = []

    def resolve(inputs, cache, *, coordinator, batch, token_adapters=None):
        index = next(
            item for item in inputs.sources if item.logical_path == "knowledge/index.md"
        )
        seen_index_hashes.append(index.sha256)
        return SimpleNamespace(
            plan={"schema_version": "compile-plan/v2", "operations": []},
            action_key=sha256_bytes(
                canonical_json_bytes([item.logical_path for item in inputs.dailies])
            ),
            cache_hit=False,
            provider_budget={
                "provider": "fake",
                "model": "fake-v1",
                "max_output_tokens": 4000,
            },
        )

    monkeypatch.setattr(compile_memory, "load_state", lambda: state)
    monkeypatch.setattr(compile_memory, "update_state", lambda mutate: mutate(state))
    monkeypatch.setattr(compile_memory, "_mark_finished", lambda *args, **kwargs: None)
    monkeypatch.setattr(compile_memory, "resolve_compile_plan", resolve)

    result = compile_memory._run(
        Namespace(file=None, all=False, dry_run=False, trigger="manual")
    )

    assert result == 0
    assert len(seen_index_hashes) == 2
    assert seen_index_hashes[0] != seen_index_hashes[1]
    assert state["compiled_daily_hashes"] == {
        first.name: sha256_bytes(first.read_bytes()),
        second.name: sha256_bytes(second.read_bytes()),
    }


def test_run_pending_compile_propagates_supplied_owner(monkeypatch):
    import compile_memory

    owner = object()
    captured = {}

    def run(args, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(compile_memory, "_run", run)

    assert compile_memory.run_pending_compile(owner=owner) == 0
    assert captured["owner"] is owner


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


def test_evidence_quote_rejects_substring_of_complete_bullet(vault):
    root, _state_root = vault
    daily = root / "knowledge/daily/2026-07-14.md"
    daily.write_text(
        "## [10:00:00] session-end | manual\n"
        "- Always reject substring citations before durable publication.\n",
        encoding="utf-8",
    )
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    plan = _semantic_plan()
    semantic = json.loads(plan["operations"][0]["content"])
    semantic["evidence"][0]["quoted_text"] = "reject substring citations"
    plan["operations"][0]["content"] = canonical_json_bytes(semantic).decode()

    with pytest.raises(ValueError, match="complete source line"):
        compile_memory.validate_compile_plan(plan, inputs)

    semantic["evidence"][0]["quoted_text"] = (
        "Always reject substring citations before durable publication."
    )
    plan["operations"][0]["content"] = canonical_json_bytes(semantic).decode()
    compile_memory.validate_compile_plan(plan, inputs)


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


def _narrowed(inputs):
    """What batching hands the writer when nothing optional fits the prompt."""
    import compile_memory

    return compile_memory._subset_compile_inputs(
        inputs, {inputs.dailies[0].part_key}, optional_paths=set()
    )


def test_index_and_log_commit_when_the_prompt_had_no_room_for_them(vault):
    """The prompt budget decides what the model reads, never what is on disk."""
    root, state_root = vault
    daily = _daily(root)
    (root / "knowledge/log.md").write_bytes(
        b"# Session Memory Log\n\n- 2026-07-01 - an earlier pass.\n"
    )
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])

    result = compile_memory.apply_compile_plan(
        _narrowed(inputs),
        _semantic_plan(),
        action_key="b" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    assert result.state == "committed"
    assert b"exact-byte-pattern" in (root / "knowledge/index.md").read_bytes()


def test_a_log_outside_the_prompt_is_appended_to_rather_than_rewritten(vault):
    """A dropped log source once became an empty before-image."""
    root, state_root = vault
    daily = _daily(root)
    (root / "knowledge/log.md").write_bytes(
        b"# Session Memory Log\n\n- 2026-07-01 - an earlier pass.\n"
    )
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])

    compile_memory.apply_compile_plan(
        _narrowed(inputs),
        _semantic_plan(),
        action_key="c" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    log = (root / "knowledge/log.md").read_bytes()
    assert b"an earlier pass." in log
    assert inputs.dailies[0].sha256.encode() in log


def test_an_absent_index_and_log_are_still_created(vault):
    """Nothing on disk means create, which is how a fresh vault compiles."""
    root, state_root = vault
    daily = _daily(root)
    (root / "knowledge/index.md").unlink()
    (root / "knowledge/log.md").unlink()
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])

    result = compile_memory.apply_compile_plan(
        _narrowed(inputs),
        _semantic_plan(),
        action_key="d" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    assert result.state == "committed"
    assert (root / "knowledge/log.md").is_file()
    assert (root / "knowledge/index.md").is_file()


def test_the_log_counts_a_page_this_repository_does_not_publish(vault):
    """`knowledge/log.md` is tracked; a private slug written there is content."""
    root, state_root = vault
    daily = _daily(root)
    (root / ".gitignore").write_text(
        "knowledge/notes/*\n!knowledge/notes/README.md\n", encoding="utf-8"
    )
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])

    compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="e" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    log = (root / "knowledge/log.md").read_text(encoding="utf-8")
    assert "exact-byte-pattern" not in log
    assert "1 unpublished page(s)" in log


def test_the_log_names_pages_in_a_vault_that_publishes_them(vault):
    """An installed vault denies nothing under notes; the names stay useful."""
    root, state_root = vault
    daily = _daily(root)
    (root / ".gitignore").write_text("cache/\nrun/\n", encoding="utf-8")
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])

    compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="f" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )

    log = (root / "knowledge/log.md").read_text(encoding="utf-8")
    assert "knowledge/notes/exact-byte-pattern.md" in log


def _quarantine_next_compile(root: Path, state_root: Path, daily: Path):
    """Drive one compile into quarantine the way the DLP boundary did."""
    import compile_memory
    import markdown_transaction

    inputs = compile_memory.snapshot_compile_inputs([daily])
    coordinator = MarkdownCoordinator(root, state_root)
    original = markdown_transaction.require_safe_publication

    def refuse(content: bytes) -> None:
        del content
        raise markdown_transaction.DLPContentBlocked("content contains protected data")

    markdown_transaction.require_safe_publication = refuse
    try:
        with pytest.raises(Exception):
            compile_memory.apply_compile_plan(
                inputs,
                _semantic_plan(),
                action_key="9" * 64,
                trigger="manual",
                coordinator=coordinator,
                completed_at="2026-07-14T12:00:00Z",
            )
    finally:
        markdown_transaction.require_safe_publication = original
    return inputs


def test_a_quarantined_attempt_does_not_lock_its_inputs_out_of_compiling(vault):
    """A refused attempt is evidence, not a life sentence for those dailies.

    The operation id comes from the inputs, so a retry after the refusal was
    fixed carries the same id with a different request hash and the coordinator
    refused it. That refusal is right — an idempotency key must never be reused
    for a different payload — so the retry takes the next attempt id instead,
    exactly as project checkpoints already do.
    """
    root, state_root = vault
    daily = _daily(root)
    _quarantine_next_compile(root, state_root, daily)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    result = compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="9" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:30:00Z",
    )

    assert result.state == "committed"
    assert (root / "knowledge/notes/exact-byte-pattern.md").is_file()


def test_a_split_day_is_recorded_by_the_whole_file_not_its_last_part(
    vault, monkeypatch
):
    """The mirror must mean what its cheap readers assume it means.

    A long day is compiled part by part. Recording the last part's digest under
    the file name made the lint, the MCP status and the compile trigger call a
    fully compiled day stale for ever — no compile would ever revisit it, so
    nothing could correct the record.
    """
    root, state_root = vault
    import compile_memory
    from memory_state import load_state

    daily = root / "knowledge/daily/2026-07-14.md"
    entry = b"<!-- llm-wiki-operation: session -->\n" + b"session text\n" * 300
    daily.write_bytes(b"# 2026-07-14\n\n" + entry * 12)
    content = daily.read_bytes()
    bounds = compile_memory._daily_part_bounds(content)
    assert len(bounds) > 1, "this day is supposed to split into parts"
    monkeypatch.setattr(
        compile_memory, "_receipt_predicate", lambda _coordinator: lambda *_a: True
    )

    compile_memory._repair_compile_mirror(object())

    mirror = load_state().get("compiled_daily_hashes", {})
    last_part = compile_memory.sha256_bytes(content[bounds[-1][0] : bounds[-1][1]])
    assert mirror["2026-07-14.md"] == compile_memory.sha256_bytes(content)
    assert mirror["2026-07-14.md"] != last_part
    del state_root


def test_a_day_without_receipts_for_every_part_is_left_alone(vault, monkeypatch):
    """Only the receipts decide; the repair merely writes down what they say."""
    root, state_root = vault
    import compile_memory
    from memory_state import load_state

    daily = root / "knowledge/daily/2026-07-15.md"
    daily.write_bytes(b"# 2026-07-15\n\nshort day\n")
    monkeypatch.setattr(
        compile_memory, "_receipt_predicate", lambda _coordinator: lambda *_a: False
    )

    compile_memory._repair_compile_mirror(object())

    assert "2026-07-15.md" not in load_state().get("compiled_daily_hashes", {})
    del state_root


def test_a_corrupt_receipt_says_which_one_and_why(vault):
    """One message for four causes explains nothing to whoever meets it.

    The live vault stopped compiling behind this message and the only way to
    learn the reason was to reproduce the failure through the reader.
    """
    root, state_root = vault
    daily = _daily(root)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="9" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:00:00Z",
    )
    digest = inputs.dailies[0].sha256
    path = root / "knowledge/daily/receipts" / f"{digest}.md"
    path.write_bytes(
        path.read_bytes().replace(b'"state":"completed"', b'"state":"broken"')
    )

    with pytest.raises(ValueError) as failure:
        compile_memory.read_compile_receipt(
            digest, MarkdownCoordinator(root, state_root)
        )

    message = str(failure.value)
    prefix, separator, reason = message.partition(": ")
    assert prefix.startswith("compile receipt is corrupt")
    assert path.name in prefix
    assert separator and reason.strip()


def test_the_retry_receipt_names_the_transaction_that_committed_it(vault):
    """A receipt is evidence only when the operation it names actually wrote it.

    The retry ordinal used to be chosen at the commit, after the receipts had
    been rendered with the refused id. Those receipts then pointed at a
    quarantined transaction, the reader called them corrupt, and every later
    compile of the same sources failed on reading them. That is how the live
    vault stopped compiling on 2026-08-22 while its own log said the compile
    had succeeded.
    """
    root, state_root = vault
    daily = _daily(root)
    _quarantine_next_compile(root, state_root, daily)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    result = compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="9" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:30:00Z",
    )
    assert result.state == "committed"

    coordinator = MarkdownCoordinator(root, state_root)
    digest = inputs.dailies[0].sha256
    record = compile_memory.read_compile_receipt(digest, coordinator)
    assert record is not None

    identity = str(record["operation_id"])
    assert "#" not in identity, "the receipt names the identity, not the attempt"
    committed = coordinator.committed_attempt(identity)
    assert committed is not None
    assert committed.state == "committed"
    assert committed.operation_id.startswith(f"{identity}#")


def test_the_quarantined_attempt_is_kept_and_named_as_the_parent(vault):
    """The refused attempt stays exactly as it was, and the retry points at it."""
    root, state_root = vault
    daily = _daily(root)
    _quarantine_next_compile(root, state_root, daily)
    import sqlite3

    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="9" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T12:30:00Z",
    )

    database = sqlite3.connect(state_root / "run" / "markdown-transactions.sqlite3")
    database.row_factory = sqlite3.Row
    rows = list(
        database.execute(
            'SELECT operation_id, state, parent_transaction_id FROM "transaction" '
            "WHERE operation_id LIKE 'compile:%' ORDER BY created_at"
        )
    )
    database.close()

    quarantined = [row for row in rows if row["state"] == "quarantined"]
    committed = [row for row in rows if row["state"] == "committed"]
    assert len(quarantined) == 1
    assert committed
    assert committed[-1]["operation_id"] != quarantined[0]["operation_id"]
    assert committed[-1]["parent_transaction_id"] is not None


def test_a_second_refusal_takes_the_next_ordinal_again(vault):
    """Attempts form a chain, so two refusals do not become a dead end either."""
    root, state_root = vault
    daily = _daily(root)
    _quarantine_next_compile(root, state_root, daily)
    _quarantine_next_compile(root, state_root, daily)
    import compile_memory

    inputs = compile_memory.snapshot_compile_inputs([daily])
    result = compile_memory.apply_compile_plan(
        inputs,
        _semantic_plan(),
        action_key="9" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-14T13:00:00Z",
    )

    assert result.state == "committed"


def test_the_retry_chain_is_bounded(vault, monkeypatch):
    """A hundred refusals is an operator's problem, not a numbering exercise."""
    root, state_root = vault
    daily = _daily(root)
    import markdown_transaction

    monkeypatch.setattr(markdown_transaction, "MAX_ATTEMPT_ORDINAL", 1)
    _quarantine_next_compile(root, state_root, daily)
    _quarantine_next_compile(root, state_root, daily)
    coordinator = MarkdownCoordinator(root, state_root)
    import sqlite3

    database = sqlite3.connect(state_root / "run" / "markdown-transactions.sqlite3")
    base = database.execute(
        'SELECT operation_id FROM "transaction" '
        "WHERE state = 'quarantined' AND operation_id NOT LIKE '%#%' LIMIT 1"
    ).fetchone()[0]
    database.close()

    with pytest.raises(ValueError, match="exhausted its quarantined retry ordinals"):
        coordinator.attempt_operation_id(base)
