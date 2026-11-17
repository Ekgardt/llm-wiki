"""Identity digests and snapshot checks every compile controller stage shares."""
from __future__ import annotations

import hashlib

import compile_progress as progress
from bounded_io import immutable_source_digest
from llm_wiki.formats.canonical import canonical_json_bytes


def snapshot_types():
    from compile_receipts import (
        DailySnapshot,
        SourceSnapshot,
    )
    from llm_wiki.compile_pipeline_sources import (
        TargetSnapshot,
    )

    return {'dailies': DailySnapshot, 'sources': SourceSnapshot,
            'targets': TargetSnapshot, 'vault_files': SourceSnapshot}


def require_snapshot_digest(content, digest):
    if immutable_source_digest(content) != digest:
        raise ValueError('snapshot content digest mismatch')


def require_original_digest(value):
    original = value.get('original_content')
    if original is not None and original != value['original_sha256']:
        raise ValueError('original snapshot content digest mismatch')


def require_input_checkpoint(checkpoint):
    if (checkpoint.stage, checkpoint.outcome) != ('snapshot_ready', 'ready'):
        raise progress.ProgressConflict('captured inputs require a ready snapshot checkpoint')
    if 'model_call' in checkpoint.details:
        raise progress.ProgressConflict('model answer is not an input snapshot')


def compile_identity_digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
