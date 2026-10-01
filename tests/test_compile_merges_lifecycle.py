"""A page update and lifecycle policy must share one publication image."""
import json

import pytest
from claims import ClaimIndex
from markdown_transaction import MarkdownCoordinator, canonical_json_bytes

from tests.test_compile_transactions import _claim_record, _daily, _semantic_plan
from tests.test_compile_transactions import vault as vault


def _existing(root):
    old = _claim_record(root, claim_id="old", value="blue", text="The prior state is blue.", authority="web")
    page = root / "knowledge/notes/exact-byte-pattern.md"
    page.write_bytes(b"---\ntype: concept\n---\n# Existing\n\n## Claims\n```json\n"
                     + canonical_json_bytes({"schema_version": "claim-ledger/v1", "claims": [old]})
                     + b"\n```\n")
    return page


def _update(vault):
    import compile_memory as compiler

    root, state = vault
    daily = _daily(root)
    page = _existing(root)
    new = _claim_record(root, claim_id="new", value="red", text="A durable exact-byte observation.", authority="user")
    plan = _semantic_plan()
    operation = plan["operations"][0]
    semantic = json.loads(operation["content"])
    semantic.update(action="update", claims=[new])
    operation.update(kind="replace", content=canonical_json_bytes(semantic).decode())
    inputs = compiler.snapshot_compile_inputs([daily])
    ClaimIndex(state, vault=root).rebuild()
    return compiler, root, state, page, semantic, inputs, plan


@pytest.mark.parametrize("use_v3", [False, True])
def test_update_supersedes_prior_claim_on_its_own_page(vault, use_v3):
    compiler, root, state, page, semantic, inputs, plan = _update(vault)
    coordinator = MarkdownCoordinator(root, state)
    kwargs = _receipt_options(compiler, inputs, use_v3)
    result = compiler.apply_compile_plan(inputs, plan, action_key="a" * 64, trigger="manual",
                                        coordinator=coordinator, **kwargs)
    assert result.state == "committed"
    ledger = json.loads(page.read_text().split("```json\n")[1].split("\n```")[0])
    assert {c["id"]: c["lifecycle"] for c in ledger["claims"]} == {"old": "superseded", "new": "active"}
    assert semantic["body_markdown"] in page.read_text()
    assert "status: superseded" not in page.read_text()

    after = page.read_bytes()
    replay = compiler.apply_compile_plan(inputs, plan, action_key="a" * 64, trigger="manual",
                                        coordinator=coordinator, **kwargs)
    assert replay.transaction_id == result.transaction_id
    assert page.read_bytes() == after


def test_concurrent_target_change_is_not_overwritten(vault, monkeypatch):
    from contradiction_pipeline import StaleLifecycleTarget

    compiler, root, state, page, semantic, inputs, plan = _update(vault)
    build = compiler._ApplyPlan._build_changes
    changed = page.read_bytes().replace(b"# Existing", b"# External writer evidence")

    def edit_after_build(publication):
        build(publication)
        page.write_bytes(changed)

    monkeypatch.setattr(compiler._ApplyPlan, "_build_changes", edit_after_build)
    with pytest.raises(StaleLifecycleTarget, match="target snapshot"):
        compiler.apply_compile_plan(inputs, plan, action_key="a" * 64, trigger="manual",
                                    coordinator=MarkdownCoordinator(root, state))
    assert page.read_bytes() == changed


def test_pending_identity_change_is_refused(vault):
    from contradiction_pipeline import (
        LifecycleTarget,
        StaleLifecycleTarget,
        _composed_lifecycle_page,
    )

    _, root, _, page, _, _, _ = _update(vault)
    raw = page.read_bytes()
    ledger = json.loads(raw.split(b"```json\n")[1].split(b"\n```")[0])
    from claims import IndexedClaim, NormalizedClaim

    target = LifecycleTarget.from_indexed(IndexedClaim("knowledge/notes/exact-byte-pattern.md", NormalizedClaim(ledger["claims"][0])))
    changed = raw.replace(b'"authority":"web"', b'"authority":"user"')
    with pytest.raises(StaleLifecycleTarget, match="identity changed"):
        _composed_lifecycle_page(raw, changed, {"old": target}, target.page, target.page)
    once = _composed_lifecycle_page(raw, raw, {"old": target}, target.page, target.page)
    assert _composed_lifecycle_page(raw, once, {"old": target}, target.page, target.page) == once


def _receipt_options(compiler, inputs, use_v3):
    if not use_v3:
        return {}
    batch = compiler.pack_compile_batches(inputs, model=None)[0]
    return {"batch": batch, "provider_budget": {"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000}}


@pytest.mark.parametrize("use_v3", [False, True])
def test_interrupted_lifecycle_publication_recovers_one_image(vault, monkeypatch, use_v3):
    compiler, root, state, page, _, inputs, plan = _update(vault)
    coordinator = MarkdownCoordinator(root, state)
    kwargs = _receipt_options(compiler, inputs, use_v3)
    original = coordinator._apply_operation
    calls = 0

    def interrupt(*args, **options):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted lifecycle publication")
        return original(*args, **options)

    monkeypatch.setattr(coordinator, "_apply_operation", interrupt)
    with pytest.raises(OSError, match="interrupted lifecycle"):
        compiler.apply_compile_plan(inputs, plan, action_key="a" * 64, trigger="manual",
                                    coordinator=coordinator, **kwargs)
    after = page.read_bytes()
    monkeypatch.setattr(coordinator, "_apply_operation", original)
    recovered = coordinator.recover()
    assert recovered[-1].state == "committed"
    assert page.read_bytes() == after
    replay = compiler.apply_compile_plan(inputs, plan, action_key="a" * 64, trigger="manual",
                                        coordinator=coordinator, **kwargs)
    assert replay.transaction_id == recovered[-1].id
    assert b'"lifecycle":"superseded"' in after
    assert b'"lifecycle":"active"' in after


def _other_subject(record):
    from reliable_memory import sha256_bytes

    record = dict(record, subject="another-project")
    semantic = {key: record[key] for key in ("subject", "relation", "value", "qualifiers", "validity")}
    record["fingerprint"] = sha256_bytes(canonical_json_bytes(semantic))
    return record


def test_multiple_claim_groups_compose_one_shared_target(vault):
    compiler, root, state, page, semantic, _, plan = _update(vault)
    old = _other_subject(_claim_record(root, claim_id="other-old", value="blue", text="The prior state is blue.", authority="web"))
    raw = page.read_bytes()
    ledger = json.loads(raw.split(b"```json\n")[1].split(b"\n```")[0])
    ledger["claims"].append(old)
    page.write_bytes(raw.split(b"```json\n")[0] + b"```json\n" + canonical_json_bytes(ledger) + b"\n```\n")
    other = _other_subject(_claim_record(root, claim_id="other-new", value="red", text="A second durable exact-byte observation.", authority="user"))
    operation = dict(semantic, action="create", slug="other-page", claims=[other])
    plan["operations"].append({"kind": "create", "path": "knowledge/notes/other-page.md", "content": canonical_json_bytes(operation).decode()})
    inputs = compiler.snapshot_compile_inputs([root / "knowledge/daily/2026-07-14.md"])
    ClaimIndex(state, vault=root).rebuild()
    coordinator = MarkdownCoordinator(root, state)
    result = compiler.apply_compile_plan(inputs, plan, action_key="c" * 64, trigger="manual", coordinator=coordinator)
    ledger = json.loads(page.read_bytes().split(b"```json\n")[1].split(b"\n```")[0])
    assert {item["id"]: item["lifecycle"] for item in ledger["claims"]} == {"old": "superseded", "other-old": "superseded", "new": "active"}
    assert semantic["body_markdown"] in page.read_text()
    assert "status: superseded" not in page.read_text()
    replay = compiler.apply_compile_plan(inputs, plan, action_key="c" * 64, trigger="manual", coordinator=coordinator)
    assert replay.transaction_id == result.transaction_id
