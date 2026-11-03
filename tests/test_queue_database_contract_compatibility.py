"""Old queue and journal entry points retain the newly separated objects."""
import compile_progress
import memory_queue
from llm_wiki import compile_journal_schema, queue_database_contract


def test_journal_facade_retains_exact_objects():
    for name in ('QUEUE_SCHEMA_VERSION', 'SCHEMA_STATEMENTS', '_require_schema', '_require_schema_object'):
        assert getattr(compile_progress, name) is getattr(compile_journal_schema, name)


def test_queue_facade_retains_exact_objects():
    names = ('_QUEUE_V3_CONTRACT', '_QUEUE_COMPILE_CONTRACT', 'QUEUE_V3_SCHEMA_SHA256',
             'QUEUE_COMPILE_SCHEMA_SHA256', '_queue_contract_for_version',
             '_require_supported_queue_contract', '_checked_queue_connection')
    for name in names:
        assert getattr(memory_queue, name) is getattr(queue_database_contract, name)
