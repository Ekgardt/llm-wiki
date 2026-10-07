"""Automatic compile cannot authorize edits to an existing decision."""
import json

import compile_memory as cm
import pytest
from markdown_transaction import MarkdownChange, MarkdownCoordinator
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.test_compile_transactions import _daily, _semantic_plan
from tests.test_compile_transactions import vault as vault


def _existing(root, content=b'---\ntype: decision\nstatus: active\n---\n# Decision\n'):
    path = root / 'knowledge/notes/exact-byte-pattern.md'
    path.write_bytes(content)
    return path


def _update_plan():
    plan = _semantic_plan()
    semantic = json.loads(plan['operations'][0]['content'])
    semantic.update(action='update', category='patterns')
    plan['operations'][0].update(kind='replace', content=canonical_json_bytes(semantic).decode())
    return plan


@pytest.mark.parametrize('content', [b'---\ntype: decision\n---\n# Old\n', b'---\r\ntype: "decision"\r\n---\r\n# Old\r\n', b"---\ntype: 'decision'\nstatus: active\n---\n# Old\n"])
def test_authentic_target_type_blocks_proposed_other_category(content):
    target = cm.TargetSnapshot('knowledge/notes/one.md', content, sha256_bytes(content))
    with pytest.raises(ValueError, match='decision'):
        cm._require_target_state({'action': 'update', 'category': 'patterns'}, target)


def test_direct_normalized_plan_validation_refuses_decision(vault):
    root, _state = vault
    _existing(root)
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    with pytest.raises(ValueError, match='decision'):
        cm.validate_compile_plan(_update_plan(), inputs)


def test_normal_apply_refuses_without_changing_decision(vault):
    root, state = vault
    path = _existing(root)
    before = path.read_bytes()
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    with pytest.raises(ValueError, match='decision'):
        cm.apply_compile_plan(inputs, _update_plan(), action_key='a'*64, trigger='auto', coordinator=MarkdownCoordinator(root, state))
    assert path.read_bytes() == before


def test_create_for_existing_decision_normalizes_to_refused_update(vault):
    root, _state = vault
    _existing(root)
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    semantic = json.loads(_semantic_plan()['operations'][0]['content'])
    operations = cm._with_snapshot_actions([semantic], inputs)
    assert operations[0]['action'] == 'update'
    with pytest.raises(ValueError, match='decision'):
        cm._normalize_plan(operations, inputs)


def test_normal_update_still_allowed_and_body_type_is_not_frontmatter(vault):
    root, state = vault
    path = _existing(root, b'---\ntype: concept\n---\n# Existing\n\ntype: decision\n')
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    assert cm.validate_compile_plan(_update_plan(), inputs)
    cm.apply_compile_plan(inputs, _update_plan(), action_key='b'*64, trigger='auto', coordinator=MarkdownCoordinator(root, state))
    assert b'The reviewed source is immutable.' in path.read_bytes()


def test_new_decision_creation_remains_allowed(vault):
    root, state = vault
    inputs = cm.snapshot_compile_inputs([_daily(root)])
    plan = _semantic_plan()
    semantic = json.loads(plan['operations'][0]['content'])
    semantic.update(category='decisions', body_section='Decision')
    plan['operations'][0]['content'] = canonical_json_bytes(semantic).decode()
    cm.apply_compile_plan(inputs, plan, action_key='c'*64, trigger='auto', coordinator=MarkdownCoordinator(root, state))
    assert b'type: decision' in (root / plan['operations'][0]['path']).read_bytes()


def test_unreadable_target_metadata_cannot_prove_mutability():
    content = b'---\ntype: decision\nbroken: [\n---\n# Old\n'
    target = cm.TargetSnapshot('knowledge/notes/one.md', content, sha256_bytes(content))
    with pytest.raises(ValueError, match='frontmatter'):
        cm._require_target_state({'action': 'update'}, target)


@pytest.mark.parametrize("status", [b"status: active", b"status: superseded\nsuperseded_by: [[successor]]"])
def test_approved_operator_transaction_remains_available(vault, status):
    root, state = vault
    path = _existing(root)
    before = path.read_bytes()
    coordinator = MarkdownCoordinator(root, state)
    transaction = coordinator.prepare([MarkdownChange.replace(path.relative_to(root).as_posix(), before.replace(b'status: active', status)+b'\nApproved editorial correction.\n')], operation_id='operator-editorial-test', preconditions={path.relative_to(root).as_posix(): sha256_bytes(before)})
    assert transaction.operations[0].before_hash == sha256_bytes(before)
    committed = coordinator.apply(transaction.id)
    assert committed.state == "committed"
    assert path.read_bytes() == before.replace(b"status: active", status)+b"\nApproved editorial correction.\n"
    inverse = coordinator.undo(transaction.id)
    assert coordinator.apply(inverse.id).state == "committed"
    assert path.read_bytes() == before
    assert b"# Decision\n" in path.read_bytes()
