"""The stable named-refusal envelope belongs to one importable class."""
import pickle

from repository_refusal import RepositoryIndexRefused


def test_reason_message_and_details_survive_the_envelope():
    refusal = RepositoryIndexRefused('outside_scope', 'Checkout is outside scope', path='a/b', retry=False)
    assert isinstance(refusal, ValueError)
    assert refusal.args == ('Checkout is outside scope',)
    assert refusal.reason == 'outside_scope'
    assert refusal.details == {'path': 'a/b', 'retry': False}
    assert refusal.as_dict() == {
        'schema_version': 'repository-index/v1', 'status': 'refused',
        'reason': 'outside_scope', 'message': 'Checkout is outside scope',
        'path': 'a/b', 'retry': False,
    }


def test_envelope_is_a_fresh_mapping():
    refusal = RepositoryIndexRefused('missing', 'Not present')
    envelope = refusal.as_dict()
    envelope['reason'] = 'changed'
    assert refusal.as_dict()['reason'] == 'missing'


def test_existing_detail_precedence_is_preserved():
    refusal = RepositoryIndexRefused('named', 'message', status='custom', schema_version='custom/v1')
    assert refusal.as_dict()['status'] == 'custom'
    assert refusal.as_dict()['schema_version'] == 'custom/v1'


def test_exception_class_has_stable_import_identity():
    assert RepositoryIndexRefused.__module__ == 'repository_refusal'
    assert pickle.loads(pickle.dumps(RepositoryIndexRefused)) is RepositoryIndexRefused
