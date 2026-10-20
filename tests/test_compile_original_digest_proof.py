"""Original digest reuse must not allow forged per-record identities."""
from dataclasses import asdict

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_daily_part_lifetime import _long_day
from tests.test_compile_streamed_archive_batches import _archive
from tests.test_compile_transactions import vault as vault


def test_replay_hashes_shared_original_once_for_record_validation(vault, monkeypatch):
    inputs = compiler.snapshot_compile_inputs([_long_day(vault[0])])
    original = inputs.dailies[0].original_content
    hashed = []
    digest = controller._blob_digest

    def observed(content):
        if content == original:
            hashed.append(len(content))
        return digest(content)

    with _archive(inputs) as reader:
        monkeypatch.setattr(controller, '_blob_digest', observed)
        assert tuple(reader.daily_records()) == inputs.dailies
    assert hashed == [len(original)]


@pytest.mark.parametrize('change', ['declared_digest', 'other_bytes', 'fake_proof'])
def test_proof_never_authorizes_a_changed_record(vault, change):
    inputs = compiler.snapshot_compile_inputs([_long_day(vault[0])])
    value = asdict(inputs.dailies[0])
    proof = controller._OriginalDigestProof(value['original_content'])
    changes = {
        'declared_digest': {'original_sha256': '0' * 64},
        'other_bytes': {'original_content': value['original_content'] + b'changed'},
        'fake_proof': {},
    }
    value.update(changes[change])
    if change == 'fake_proof':
        proof = {'content': value['original_content'], 'digest': value['original_sha256']}
    with pytest.raises(ValueError):
        controller._restored_archive_record(compiler.DailySnapshot, value, original_proof=proof)


def test_digest_proof_rejects_mutable_content():
    with pytest.raises(ValueError, match='snapshot content must be bytes'):
        controller._OriginalDigestProof(bytearray(b'can change'))
