from __future__ import annotations

import json

import pytest
from markdown_transaction import MarkdownCoordinator, TransactionFailure
from reliable_memory import canonical_json_bytes

from tests.test_compile_transactions import _daily, _semantic_plan
from tests.test_compile_transactions import vault as _vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return _vault.__wrapped__(tmp_path, monkeypatch)


def _compile(root, coordinator, *, slug, key):
    import compile_memory as compile

    inputs = compile.snapshot_compile_inputs([root / "knowledge/daily/2026-07-14.md"])
    batch = compile.pack_compile_batches(inputs, model=None)[0]
    plan = _semantic_plan()
    operation = plan["operations"][0]
    semantic = json.loads(operation["content"])
    semantic["slug"] = slug
    operation["path"] = f"knowledge/notes/{slug}.md"
    operation["content"] = canonical_json_bytes(semantic).decode()
    return compile.apply_compile_plan(
        batch.inputs, plan, action_key=key * 64, trigger="manual",
        coordinator=coordinator, batch=batch,
        provider_budget={"provider": "fake", "model": "test", "max_output_tokens": 4000},
    )


def _refused_then_committed(vault, monkeypatch):
    root, state = vault
    _daily(root)
    coordinator = MarkdownCoordinator(root, state)
    original = coordinator.prepare

    def refuse(*args, **kwargs):
        conditions = dict(kwargs["preconditions"])
        conditions["knowledge/notes/a-page-that-does-not-exist.md"] = "0" * 64
        kwargs["preconditions"] = conditions
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(coordinator, "prepare", refuse)
        with pytest.raises(TransactionFailure):
            _compile(root, coordinator, slug="old-proposal", key="a")
    _compile(root, coordinator, slug="accepted-result", key="b")
    return root, state, coordinator


def test_an_exact_committed_source_resolves_a_different_old_page_plan(vault, monkeypatch):
    import doctor

    root, state, coordinator = _refused_then_committed(vault, monkeypatch)
    with coordinator._connect() as database:
        unresolved = doctor._unresolved_quarantine(
            database, {row[1] for row in database.execute('PRAGMA table_info("transaction")')},
            doctor._CompiledDaySupersession(root, state),
        )
    assert not (root / "knowledge/notes/old-proposal.md").exists()
    assert (root / "knowledge/notes/accepted-result.md").is_file()
    assert unresolved == 0


def _old_attempts(coordinator):
    with coordinator._connect() as database:
        return [row[0] for row in database.execute('SELECT id FROM "transaction" WHERE state=\'quarantined\'')]


def _erase_receipt(root, coordinator):
    next((root / "knowledge/daily/receipts").glob("v3-*.md")).unlink()


def _alter_receipt(root, coordinator):
    receipt = next((root / "knowledge/daily/receipts").glob("v3-*.md"))
    receipt.write_bytes(receipt.read_bytes() + b"changed bytes\n")


def _uncommitted_authority(root, coordinator):
    with coordinator._connect() as database:
        database.execute('UPDATE "transaction" SET state=\'prepared\' WHERE state=\'committed\'')


def _alter_operation_authority(root, coordinator):
    with coordinator._connect() as database:
        database.execute("UPDATE operation SET after_hash=? WHERE path=?", ("0" * 64, "knowledge/notes/accepted-result.md"))


def _alter_staged_image(root, coordinator):
    state = coordinator.state_root
    for identifier in _old_attempts(coordinator):
        plan = json.loads((state / "run/transactions" / identifier / "plan.json").read_bytes())
        receipts = [item for item in plan["operations"] if item["path"].startswith("knowledge/daily/receipts/")]
        for operation in receipts:
            (state / "run/transactions" / identifier / operation["after"]["artifact"]).write_bytes(b"changed staged image")


def _noncompile_attempt(root, coordinator):
    with coordinator._connect() as database:
        database.execute('UPDATE "transaction" SET operation_id=\'other:\'||id WHERE state=\'quarantined\'')


@pytest.mark.parametrize("invalidate", [
    _erase_receipt, _alter_receipt, _uncommitted_authority,
    _alter_operation_authority, _alter_staged_image, _noncompile_attempt,
])
def test_incomplete_or_unverified_outcomes_keep_the_refusal(vault, monkeypatch, invalidate):
    import doctor

    root, state, coordinator = _refused_then_committed(vault, monkeypatch)
    identifiers = _old_attempts(coordinator)
    invalidate(root, coordinator)
    with coordinator._connect() as database:
        assert all(not doctor._compile_snapshot_was_written(database, identifier, root, state) for identifier in identifiers)


def test_decoded_images_keep_the_shared_format_and_evidence_budget():
    import lzma

    from markdown_transaction import _decoded_image_bytes

    raw = b"a verified receipt"
    assert _decoded_image_bytes(lzma.compress(raw), max_bytes=len(raw)) == raw
    assert _decoded_image_bytes(raw, max_bytes=len(raw)) == raw
    assert _decoded_image_bytes(lzma.compress(raw[:2]) + lzma.compress(raw[2:]), max_bytes=len(raw)) == raw
    with pytest.raises(ValueError, match="budget"):
        _decoded_image_bytes(lzma.compress(raw), max_bytes=len(raw) - 1)


def test_historical_receipt_day_reader_decodes_the_shared_image_format(tmp_path):
    import lzma

    import doctor

    state = tmp_path / "state"
    directory = state / "run/transactions/" / ("a" * 32)
    (directory / "after").mkdir(parents=True)
    raw = b'```json\n{"source":{"logical_path":"knowledge/daily/2026-07-14.md"}}\n```'
    (directory / "after/000000.bin").write_bytes(lzma.compress(raw))
    reader = doctor._CompiledDaySupersession(tmp_path / "vault", state)
    assert reader._staged_day(directory, "after/000000.bin") == "knowledge/daily/2026-07-14.md"
