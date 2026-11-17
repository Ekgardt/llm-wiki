"""Helpers shared by test_compile_native_journal_parse_belongs_to_the_immutable_source.py and the test files importing them."""

import compile_pipeline
from llm_wiki import compile_pipeline_sources

from tests.native_compile_uses_whole_container_helpers import _native_inputs


def _two_units(root, monkeypatch):
    inputs, _ = _native_inputs(root, monkeypatch, "A" * 18000 + "\nActual fact.")
    path = root / inputs.dailies[0].logical_path
    prefix, marker, body = path.read_bytes().partition(b"\n<!-- llm-wiki-operation:")
    block = marker + body
    separator = b"\n## [00:00:01] Separator\n" + b"Z" * (compile_pipeline.MAX_DAILY_PART_BYTES * 2) + b"\n"
    path.write_bytes(prefix + block + separator + block)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([path])
    frames = inputs.dailies[0].native_frames
    assert len(frames) == 2
    keys = [{part.part_key for part in inputs.dailies
             if part.byte_start < frame.byte_end and part.byte_end > frame.byte_start}
            for frame in frames]
    return inputs, keys
