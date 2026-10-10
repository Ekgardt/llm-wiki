"""Citation count cannot discard complete evidence; indices bind the actual list."""
import json

import compile_memory
import pytest

from tests.test_compile_claims_producer import _candidate, _evidence, _operation


@pytest.mark.parametrize('count', [33, 36, 64])
def test_complete_evidence_and_last_claim_survive_draft_validation(count):
    operation = _operation([_candidate(evidence_index=count - 1)])
    operation['evidence'] = [_evidence() for _ in range(count)]
    operations = compile_memory._draft_operations(json.dumps({'operations': [operation]}))
    assert operations == [operation]
    compile_memory._require_evidence_shape(operations[0]['evidence'])
    assert compile_memory._claim_evidence_item(operations[0], count - 1) == _evidence()


@pytest.mark.parametrize('index', [-1, 36, True, '35', 35.0])
def test_claim_index_still_requires_an_actual_integer_reference(index):
    operation = {'evidence': [_evidence() for _ in range(36)]}
    with pytest.raises(ValueError, match='evidence index'):
        compile_memory._claim_evidence_item(operation, index)


@pytest.mark.parametrize('evidence', [None, {}, '', []])
def test_operation_still_requires_a_nonempty_evidence_list(evidence):
    with pytest.raises(ValueError, match='requires evidence'):
        compile_memory._require_evidence_shape(evidence)
