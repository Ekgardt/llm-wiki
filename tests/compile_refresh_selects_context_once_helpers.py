"""Helpers shared by test_compile_refresh_selects_context_once.py and the test files importing them."""

from dataclasses import replace

import compile_pipeline
import llm_client as client

from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs
from tests.compile_paths import point_compile_path


def _configure_run(monkeypatch):
    candidates = client.provider_candidates('fake', max_tokens=4000)
    point_compile_path(monkeypatch, 'provider_candidates', lambda *args, **kwargs: candidates)
    point_compile_path(monkeypatch, 'probe_candidate', lambda candidate: True)
    return candidates


def _planned(monkeypatch):
    _configure_run(monkeypatch)
    batches, refused = compile_pipeline.pack_for_run(_inputs(optional=True), float('inf'))
    assert not refused
    return batches[0]


def _alter_refresh(batch, field):
    changes = {
        'manifest': lambda: replace(batch, manifest=()),
        'model': lambda: replace(batch, planning_model='another-model'),
        'candidates': lambda: replace(batch, planning_candidates=()),
        'budget': lambda: replace(batch, packing=replace(batch.packing, max_input_tokens=batch.packing.max_input_tokens + 1)),
    }
    return changes[field]()
