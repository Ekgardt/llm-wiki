"""Original digest reuse must not allow forged per-record identities."""
from dataclasses import asdict

import bounded_io
import compile_controller as controller
import compile_pipeline
import compile_receipts
import pytest
from llm_wiki import compile_pipeline_sources

from tests.compile_daily_part_lifetime_helpers import _long_day
from tests.compile_streamed_archive_batches_helpers import _archive, _materialized_daily
from tests.compile_transactions_helpers import vault as vault


def test_replay_hashes_shared_original_once_for_record_validation(vault, monkeypatch):
    inputs = compile_pipeline_sources.snapshot_compile_inputs([_long_day(vault[0])])
    original = inputs.dailies[0].original_content
    hashed = []
    digest = bounded_io.verified_original_blocks

    def observed(stream, size, expected, deadline, chunk_size):
        if expected == compile_pipeline.sha256_bytes(original):
            hashed.append(size)
        return digest(stream, size, expected, deadline, chunk_size)

    with _archive(inputs) as reader:
        monkeypatch.setattr(bounded_io, 'verified_original_blocks', observed)
        assert tuple(_materialized_daily(part) for part in reader.daily_records()) == inputs.dailies
    assert hashed == [len(original)]


@pytest.mark.parametrize('change', ['declared_digest', 'other_bytes', 'fake_proof'])
def test_proof_never_authorizes_a_changed_record(vault, change):
    inputs = compile_pipeline_sources.snapshot_compile_inputs([_long_day(vault[0])])
    value = asdict(inputs.dailies[0])
    proof = controller.OriginalDigestProof(value['original_content'])
    changes = {
        'declared_digest': {'original_sha256': '0' * 64},
        'other_bytes': {'original_content': value['original_content'] + b'changed'},
        'fake_proof': {},
    }
    value.update(changes[change])
    if change == 'fake_proof':
        proof = {'content': value['original_content'], 'digest': value['original_sha256']}
    with pytest.raises(ValueError):
        controller.restored_archive_record(compile_receipts.DailySnapshot, value, original_proof=proof)


def test_digest_proof_rejects_mutable_content():
    with pytest.raises(ValueError, match='snapshot content must be bytes'):
        controller.OriginalDigestProof(bytearray(b'can change'))
