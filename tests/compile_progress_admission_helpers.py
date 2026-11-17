"""Helpers shared by test_compile_progress_admission.py and the test files importing them."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3

import compile_progress as progress
import compile_progress_migration as migration
import pytest
import queue_core
import reliability_v3_validation
from llm_wiki import adoption_admission
from reliable_memory import canonical_json_bytes

from tests.legacy_adopted_vault import adopt_legacy as adopt


def _write_record(state, value):
    (state / 'run/reliability-v3-adopted.json').write_bytes(canonical_json_bytes(value))


@pytest.fixture
def upgraded(tmp_path):
    root, state = adopt(tmp_path)
    queue = queue_core.active_memory_queue(root, state)
    queue.enqueue('compile', 1, {'daily': 'retained'})
    candidate = state / 'run/test-copy.sqlite3'
    with contextlib.closing(sqlite3.connect(candidate)) as destination:
        with queue.connection() as source:
            source.backup(destination)
    candidate.chmod(0o600)
    migration.migrate_candidate(candidate, state_root=state)
    previous = json.loads((state / 'run/reliability-v3-adopted.json').read_bytes())
    os.replace(candidate, queue.db_path)
    active = reliability_v3_validation.artifact_record(queue.db_path, state, adoption_admission.MAX_OPERATIONAL_DB_BYTES)
    schema = {'base': queue_core.QUEUE_V3_SCHEMA_SHA256, 'version': 4,
              'journal': list(progress.SCHEMA_STATEMENTS)}
    record = {
        'schema_version': 'reliability-v3-adoption/v2', 'previous': previous,
        'previous_sha256': hashlib.sha256(canonical_json_bytes(previous)).hexdigest(),
        'queue_active': active,
        'queue_schema_sha256': hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
        'operation_id': 'a' * 64, 'backup_manifest_sha256': 'b' * 64,
    }
    _write_record(state, record)
    return root, state, record
