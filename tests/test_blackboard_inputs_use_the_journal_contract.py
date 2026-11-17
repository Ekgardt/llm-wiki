from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import blackboard
import pytest

from tests.blackboard_helpers import blackboard_vault as blackboard_vault


@pytest.mark.parametrize('field', ('task', 'resources', 'claim_json'))
def test_claim_round_trip_preserves_inputs_past_former_bound(blackboard_vault, field):
    task = '\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 ' * 600
    agent = 'agent-' + '\u0438\u043c\u044f' * 50
    resource = '/'.join(['segment'] * 80)
    values = {'task': 'inspect code', 'agent': 'codex', 'resources': ['src/a.py']}
    updates = {'task': {'task': task}, 'agent': {'agent': agent},
               'resource': {'resources': [resource]},
               'resources': {'resources': [f'src/{index}.py' for index in range(65)]},
               'claim_json': {'task': '\u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 ' * 2200}}
    values.update(updates[field])
    now = datetime(2026, 10, 2, tzinfo=UTC)
    claim = blackboard.claim_task('demo', **values, now=now)
    restored = blackboard.claim_from_json(json.dumps(blackboard.claim_payload(claim), ensure_ascii=False))
    assert restored == claim
    assert claim.task == values['task']
    assert claim.agent == values['agent']
    assert claim.resources == tuple(sorted(values['resources']))
    blackboard.complete_task('demo', restored, now)
    assert blackboard.get_status('demo')['completed_tasks'] == 1


@pytest.mark.parametrize('changes', ({'agent': '\u0438\u043c\u044f' * 50}, {'resources': ['/'.join(['segment'] * 80)]}))
def test_existing_database_contract_is_refused_before_request(blackboard_vault, changes):
    vault, _ = blackboard_vault
    values = {'task': 'inspect code', 'agent': 'codex', 'resources': ['src/a.py']}
    values.update(changes)
    with pytest.raises(ValueError, match='size limit'):
        blackboard.claim_task('demo', **values)
    assert not (vault / 'knowledge/projects/demo/.blackboard/tasks.jsonl').exists()


def test_signal_preserves_long_message_and_agent_names(blackboard_vault):
    vault, _ = blackboard_vault
    sender = 'worker-' + '\u0438\u043c\u044f' * 50
    recipient = 'reviewer-' + '\u0438\u043c\u044f' * 50
    message = '\u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d\u043d\u044b\u0439 \u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 ' * 300
    blackboard.send_signal('demo', sender, recipient, message)
    records = blackboard.read_jsonl(vault / 'knowledge/projects/demo/.blackboard/signals.jsonl')
    assert records[0]['from'] == sender
    assert records[0]['to'] == recipient
    assert records[0]['message'] == message


def test_journal_refusal_precedes_claim_ownership(blackboard_vault, monkeypatch):
    monkeypatch.setattr(blackboard, 'MAX_KNOWLEDGE_TARGET_BYTES', 64)
    with pytest.raises(ValueError):
        blackboard.claim_task('demo', 'x' * 65, 'codex', resources=['src/a.py'])
    with blackboard.blackboard_coordinator()._connect() as database:
        assert database.execute('SELECT count(*) FROM blackboard_claims').fetchone()[0] == 0


def test_complete_record_budget_is_checked_before_request(blackboard_vault, monkeypatch):
    vault, _ = blackboard_vault
    monkeypatch.setattr(blackboard, 'MAX_KNOWLEDGE_TARGET_BYTES', 64)
    with pytest.raises(ValueError, match='record exceeds its size limit'):
        blackboard.claim_task('demo', 'task', 'codex', resources=['src/a.py'])
    assert not (vault / 'knowledge/projects/demo/.blackboard/tasks.jsonl').exists()


def test_claim_json_refuses_past_existing_journal_budget(monkeypatch):
    monkeypatch.setattr(blackboard, 'MAX_KNOWLEDGE_TARGET_BYTES', 64)
    with pytest.raises(ValueError, match='size limit'):
        blackboard.claim_from_json(' ' * 65)


def test_busy_resource_lookup_does_not_depend_on_sql_variable_count(blackboard_vault):
    with blackboard.blackboard_coordinator()._connect() as database:
        database.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
        resources = tuple(f'src/{index}.py' for index in range(65))
        assert blackboard.busy_claim_rows(database, 'demo', resources) == []
