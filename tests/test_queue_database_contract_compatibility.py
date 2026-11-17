"""Old queue and journal entry points retain the newly separated objects."""
import compile_progress
import queue_core
from llm_wiki import compile_journal_schema, queue_database_contract


def test_journal_facade_retains_exact_objects():
    for name in ('QUEUE_SCHEMA_VERSION', 'SCHEMA_STATEMENTS', 'require_schema', 'require_schema_object'):
        assert getattr(compile_progress, name) is getattr(compile_journal_schema, name)


def test_queue_facade_retains_exact_objects():
    names = ('QUEUE_V3_CONTRACT', 'QUEUE_COMPILE_CONTRACT', 'QUEUE_V3_SCHEMA_SHA256',
             'QUEUE_COMPILE_SCHEMA_SHA256', 'queue_contract_for_version',
             'require_supported_queue_contract', 'checked_queue_connection')
    for name in names:
        assert getattr(queue_core, name, getattr(queue_database_contract, name)) is getattr(queue_database_contract, name)
