"""Helpers shared by test_compile_keeps_physically_overlapping_existing_context.py and the test files importing them."""

import compile_pipeline
import compile_receipts
from llm_wiki import compile_pipeline_operations, compile_pipeline_sources

from tests.compile_claims_producer_helpers import QUOTE, _daily, _evidence


def _overlap_inputs(root, newline="\n"):
    daily = _daily(root)
    daily.write_bytes(daily.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline.encode()))
    initial = compile_pipeline_sources.snapshot_compile_inputs([daily])
    binding, _block = compile_pipeline_operations.bound_evidence_block(_evidence(), initial)
    # Keep this admitted fixture below the measured whole-prompt budget;
    # the separate 38 KB case must still refuse without verified capacity.
    note = root / 'knowledge/notes/project-rule.md'
    note.write_text('---\ntype: decision\n---\n# Accepted project boundary\n' + 'z' * 17000 + '\n\nEvidence: ' + binding['reference'] + '\n', encoding='utf-8', newline=newline)
    other = root / 'knowledge/notes/lexical.md'
    other.write_text('---\ntype: pattern\n---\n' + (QUOTE + '\n') * 160, encoding='utf-8', newline=newline)
    return compile_pipeline_sources.snapshot_compile_inputs([daily]), note.read_bytes()


def _large_required(root):
    inputs, original = _overlap_inputs(root)
    note = root / 'knowledge/notes/project-rule.md'
    note.write_bytes(original.replace(b'z' * 17000, b'z' * 38000))
    return compile_pipeline_sources.snapshot_compile_inputs([root / inputs.dailies[0].logical_path])


def _part_version_inputs(root):
    from dataclasses import replace

    from evidence_resolver import EvidenceRef

    daily = _daily(root)
    daily.write_bytes(daily.read_bytes() + b'Padding.\n' * 1100 + b'\n## [11:00:00] session-end | manual\nTail verified fact.\n' + b'Tail padding.\n' * 750)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    part = inputs.dailies[-1]
    start = part.content.index(b'Tail verified fact.')
    reference = EvidenceRef('2026-07-14', part.sha256, '11:00:00', start, start + len(b'Tail verified fact.'))
    note = root / 'knowledge/notes/part-version.md'
    note.write_text('---\ntype: decision\n---\nEvidence: ' + str(reference) + '\n')
    current = compile_pipeline_sources.snapshot_compile_inputs([daily])
    return replace(current, dailies=(current.dailies[-1],)), reference


def _native_context(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.native_compile_uses_whole_container_helpers import _native_inputs, _native_operation

    inputs, _snapshot = _native_inputs(tmp_path, monkeypatch, 'A' * 18000 + '\nTail.')
    operation = _native_operation(inputs, 'Tail.')
    binding, _block = compile_pipeline_operations.bound_evidence_block(operation['evidence'][0], inputs)
    content = ('---\ntype: decision\n---\nEvidence: ' + binding['reference'] + '\n').encode()
    source = compile_receipts.SourceSnapshot('knowledge/notes/native-rule.md', content, compile_pipeline.sha256_bytes(content))
    return replace(inputs, sources=(*inputs.sources, source)), content


def _repeated_part_inputs(root):
    from evidence_resolver import EvidenceRef

    daily = _daily(root)
    prefix = b'\n## [11:00:00] session-end | manual\nRepeated verified fact.\n'
    block = prefix + b'x' * (compile_pipeline.MAX_DAILY_PART_BYTES - len(prefix) - 1) + b'\n'
    daily.write_bytes(block * 2)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    first, second = inputs.dailies
    assert first.content == second.content == block
    start = block.index(b'Repeated verified fact.')
    reference = EvidenceRef('2026-07-14', first.sha256, '11:00:00', start, start + len(b'Repeated verified fact.'))
    note = root / 'knowledge/notes/repeated-part.md'
    note.write_text('---\ntype: decision\n---\nEvidence: ' + str(reference) + '\n')
    return compile_pipeline_sources.snapshot_compile_inputs([daily]), reference
