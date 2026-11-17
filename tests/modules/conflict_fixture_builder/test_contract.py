"""Pure row selection keeps its existing first-identifier prefix semantics."""
import subprocess
import sys

import pytest

from benchmark import build_conflict_fixture as builder


@pytest.mark.parametrize('row', [None, [], {}, {'metadata': None}, {'metadata': []}])
def test_non_mapping_row_or_metadata_does_not_match(row):
    assert builder._row_matches(row, 'target') is False


@pytest.mark.parametrize('identifiers', [None, [], 'target', b'target', 12])
def test_only_identifier_sequences_are_candidates(identifiers):
    row = {'metadata': {'qa_pair_ids': identifiers}}
    assert builder._row_matches(row, 'target') is False


@pytest.mark.parametrize('identifiers, prefix, matches', [
    (['target_suffix'], 'target', True),
    (['other', 'target_suffix'], 'target', False),
    ([123], '12', True),
    (['target'], 'TARGET', False),
])
def test_only_first_identifier_is_coerced_and_prefix_matched(identifiers, prefix, matches):
    assert builder._row_matches({'metadata': {'qa_pair_ids': identifiers}}, prefix) is matches


def test_import_does_not_load_pyarrow():
    program = 'import sys; import benchmark.build_conflict_fixture; assert "pyarrow" not in sys.modules'
    result = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
