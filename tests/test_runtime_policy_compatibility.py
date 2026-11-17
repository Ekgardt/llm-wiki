"""Existing runtime policy imports keep the same function objects."""
import pickle

import pytest
import reliable_memory
from llm_wiki import restricted_paths, sqlite_pragmas


def test_path_public_pickle_identity_is_preserved():
    function = restricted_paths.restricted_relative_path
    assert reliable_memory.restricted_relative_path is function
    assert function.__module__ == 'reliable_memory'
    assert pickle.loads(pickle.dumps(function)) is function


@pytest.mark.parametrize('name', ['apply_operational_pragmas', 'pragma_integer', 'require_pragma',
                                  'validate_or_initialize_operational_contract',
                                  'initialized_contract_identity', 'apply_readonly_operational_pragmas'])
def test_pragma_old_names_are_identity_aliases(name):
    assert getattr(reliable_memory, name) is getattr(sqlite_pragmas, name)
