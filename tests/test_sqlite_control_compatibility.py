"""Historical import/pickle identity survives new SQLite control ownership."""
import pickle

import pytest
import reliable_memory
from llm_wiki import sqlite_deadlines, sqlite_migrations


@pytest.mark.parametrize('name', ['operational_deadline_active', 'operational_busy_ms',
                                  'configure_operational_deadline', 'refresh_operational_deadline'])
def test_deadline_function_identity_and_pickle_name(name):
    function = getattr(sqlite_deadlines, name)
    assert getattr(reliable_memory, name) is function
    assert function.__module__ == 'reliable_memory'
    assert pickle.loads(pickle.dumps(function)) is function


def test_migration_function_identity_and_pickle_name():
    function = sqlite_migrations.run_resumable_migration
    assert reliable_memory.run_resumable_migration is function
    assert function.__module__ == 'reliable_memory'
    assert pickle.loads(pickle.dumps(function)) is function


def test_existing_clock_patch_point_still_names_same_module():
    assert reliable_memory.time is sqlite_deadlines.time
