"""Real source fragments must fit without implicit repeated whole-page expansion."""
from __future__ import annotations

from pathlib import Path

import mcp_server
from context_budget import ContextBudget
from context_compiler import compile_context
from corpus_snapshot import collect_corpus


def _snapshot(root: Path, body: str):
    target = root / 'knowledge/notes/evidence.md'
    target.parent.mkdir(parents=True)
    target.write_text('---\ntype: concept\n---\n# Evidence\n\n'+body,encoding='utf-8')
    return collect_corpus(root)


def _selection(snapshot):
    return {'sources':snapshot.sources,'chunks':snapshot.chunks}


def _l2_spans(compiled):
    return [(item.byte_start,item.byte_end) for item in compiled.items if item.representation=='l2']


def test_context_keeps_all_selected_fragments_under_their_actual_byte_budget(tmp_path):
    snapshot=_snapshot(tmp_path, ''.join(f'## Fact {number}\n\nFact {number} must survive with its own evidence.\n\n' for number in range(4)))
    reference=compile_context(snapshot,evidence_chunk_ids=[c.id for c in snapshot.chunks],shortlist=[s.record.logical_id for s in snapshot.sources],budget=ContextBudget(None,10000,0,0),small_parent_chars=0,large_parent_subtree_chars=0)
    budget=reference.trace.packing.packed_tokens

    actual=mcp_server._compiled_context(snapshot,_selection(snapshot),budget,None)

    assert sorted(_l2_spans(actual))==sorted((c.byte_start,c.byte_end) for c in snapshot.chunks)
    assert actual.trace.packing.packed_tokens<=budget
    assert len(set(_l2_spans(actual)))==len(snapshot.chunks)


def test_default_compilation_keeps_the_requested_evidence_span(tmp_path):
    snapshot=_snapshot(tmp_path,'## Requested\n\nSelected fact.\n\n## Other\n\nUnrequested fact.\n')
    selected=next(c for c in snapshot.chunks if c.heading_ancestry[-1]=='Requested')

    actual=compile_context(snapshot,evidence_chunk_ids=[selected.id])

    assert _l2_spans(actual)==[(selected.byte_start,selected.byte_end)]
    item=next(i for i in actual.items if i.representation=='l2')
    assert 'Selected fact.' in item.text
    assert 'Unrequested fact.' not in item.text


def test_an_explicit_expansion_budget_can_hold_the_whole_neighbor(tmp_path):
    neighbor='This neighboring evidence stays complete. '*8
    snapshot=_snapshot(tmp_path,'## Requested\n\nSelected fact.\n\n## Neighbor\n\n'+neighbor+'\n\n## Stop\n\nDo not expand twice.\n')
    selected=next(c for c in snapshot.chunks if c.heading_ancestry[-1]=='Requested')
    source=snapshot.sources[0]

    actual=compile_context(snapshot,evidence_chunk_ids=[selected.id],small_parent_chars=0,large_parent_subtree_chars=len(source.content))

    item=next(i for i in actual.items if i.representation=='l2')
    assert neighbor in item.text
    assert 'Do not expand twice.' not in item.text
    assert source.content[item.byte_start:item.byte_end].decode() in item.text
