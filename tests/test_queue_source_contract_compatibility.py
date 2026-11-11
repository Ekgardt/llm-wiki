"""Old queue entry points preserve source identity behavior and object identity."""
import json

import pytest
import queue_core
import queue_core as queue
from llm_wiki import queue_sources as source


@pytest.mark.parametrize('name', ['validate_source_identity', 'validate_failure_identity', 'source_identity_from_payload'])
def test_static_entry_is_the_same_function(name):
    assert getattr(queue_core.MemoryQueue, f'_{name}') is getattr(source, name)


def test_migration_still_refuses_ambiguous_identity():
    payload = [{'source_path': 'knowledge/daily/2026-11-02.md', 'hash': 'a' * 64},
               {'source_path': 'knowledge/daily/2026-11-03.md', 'hash': 'b' * 64}]
    with pytest.raises(queue.OperationalDatabaseContractError) as caught:
        queue.queue_v3_source_links(json.dumps(payload).encode())
    assert caught.value.code == 'queue_v2_source_identity_ambiguous'


def test_migration_resolves_one_daily_identity():
    result = queue.queue_v3_source_links(json.dumps({'daily_id': '2026-11-02', 'hash': 'a' * 64}).encode())
    assert result == (('knowledge/daily/2026-11-02.md', 'a' * 64),)
